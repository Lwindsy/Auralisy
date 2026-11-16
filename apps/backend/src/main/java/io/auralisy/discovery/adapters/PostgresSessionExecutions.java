package io.auralisy.discovery.adapters;

import io.auralisy.discovery.api.SessionExecutions;
import io.auralisy.persistence.PersistenceRejected;
import io.auralisy.persistence.ShortTransactions;
import io.auralisy.session.api.SessionStore;
import java.util.HashSet;
import java.util.List;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

@Repository
public class PostgresSessionExecutions implements SessionExecutions {
    private final JdbcTemplate jdbc;
    private final SessionStore owners;

    public PostgresSessionExecutions(JdbcTemplate jdbc, SessionStore owners) {
        this.jdbc = jdbc;
        this.owners = owners;
    }

    @Override
    public void invalidate(List<String> sessions, String status) {
        ShortTransactions.requireWrite();
        if (!java.util.Set.of("SUPERSEDED", "CANCELLED", "EXPIRED").contains(status))
            throw new IllegalArgumentException("INVALID_TERMINAL_STATE");
        var ids = sessions.stream().distinct().sorted().toList();
        var subjects = new HashSet<String>();
        for (String id : ids)
            subjects.addAll(
                    jdbc.queryForList(
                            "SELECT subject_id FROM discovery.session WHERE id=?",
                            String.class,
                            id));
        if (subjects.size() > 1) throw new PersistenceRejected("LOCK_ORDER_VIOLATION");
        if (subjects.isEmpty()) return;
        String subject = subjects.iterator().next();
        if (!owners.lockOwner(subject)) return;
        for (String id : ids) {
            jdbc.update(
                    "UPDATE discovery.discovery_intent SET status=?,revision=revision+1 WHERE session_id=? AND subject_id=? AND status IN ('WAITING','DISPATCHING','RUNNING')",
                    status,
                    id,
                    subject);
            jdbc.update(
                    "UPDATE discovery.execution e SET cancellation_requested=true,revision=e.revision+1 FROM discovery.session s WHERE e.session_id=? AND e.session_id=s.id AND s.subject_id=? AND e.physical_released_at IS NULL",
                    id,
                    subject);
        }
    }

    @Override
    public boolean unreleased(List<String> sessions) {
        for (String id : sessions)
            if (jdbc.queryForObject(
                            "SELECT count(*) FROM discovery.execution WHERE session_id=? AND physical_released_at IS NULL",
                            Long.class,
                            id)
                    > 0) return true;
        return false;
    }

    @Override
    public void invalidateSubject(String subject, String status) {
        ShortTransactions.requireWrite();
        if (!java.util.Set.of("CANCELLED", "EXPIRED").contains(status))
            throw new IllegalArgumentException("INVALID_TERMINAL_STATE");
        if (!owners.lockOwner(subject)) return;
        jdbc.update(
                "UPDATE discovery.discovery_intent SET status=?,revision=revision+1 WHERE subject_id=? AND status IN ('WAITING','DISPATCHING','RUNNING')",
                status,
                subject);
        jdbc.update(
                "UPDATE discovery.execution e SET cancellation_requested=true,revision=e.revision+1 FROM discovery.discovery_intent i WHERE e.run_id=i.id AND i.subject_id=? AND e.physical_released_at IS NULL",
                subject);
    }
}
