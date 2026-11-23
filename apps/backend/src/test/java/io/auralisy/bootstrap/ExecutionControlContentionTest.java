package io.auralisy.bootstrap;

import static io.auralisy.contracts.CanonicalJson.JSON;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import io.auralisy.budget.api.BudgetAuthority;
import io.auralisy.discovery.application.Recommendations;
import io.auralisy.persistence.DbDiagnostics;
import io.auralisy.persistence.ShortTransactions;
import jakarta.servlet.RequestDispatcher;
import java.sql.SQLException;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.jdbc.UncategorizedSQLException;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;

class ExecutionControlContentionTest {
    private static final String EXECUTION = "01234567-89ab-cdef-0123-456789abcdef";
    private final WireProtocol wire = mock(WireProtocol.class);
    private final HttpErrors errors = new HttpErrors(JSON, wire);
    private final Recommendations recommendations = mock(Recommendations.class);
    private final BudgetAuthority budget = mock(BudgetAuthority.class);
    private final ShortTransactions tx = mock(ShortTransactions.class);
    private final DbDiagnostics diagnostics = mock(DbDiagnostics.class);
    private final ExecutionControlController controller =
            new ExecutionControlController(
                    null, budget, null, wire, errors, recommendations, tx, diagnostics);

    private MockHttpServletRequest request(String message, String payload) {
        when(wire.valid(any())).thenReturn(true);
        when(wire.revision()).thenReturn("0.32");
        var request = new MockHttpServletRequest("PUT", "/internal/v1/test");
        request.setContent(
                ("{\"message_type\":\""
                                + message
                                + "\",\"request_id\":\"test-request\",\"payload\":"
                                + payload
                                + "}")
                        .getBytes(java.nio.charset.StandardCharsets.UTF_8));
        return request;
    }

    private static UncategorizedSQLException sqlFailure(String state) {
        return new UncategorizedSQLException("fixture", null, new SQLException("fixture", state));
    }

    private MockHttpServletResponse delivery(String kind, String state, boolean replayable)
            throws Exception {
        var request =
                request(
                        "Delivery",
                        "{\"kind\":\"" + kind + "\",\"delivery_id\":\"" + EXECUTION + ":1\"}");
        var response = new MockHttpServletResponse();
        var failure = sqlFailure(state);
        when(recommendations.deliver(eq(EXECUTION), anyString())).thenThrow(failure);
        if (replayable) {
            assertNull(controller.delivery(EXECUTION, EXECUTION + ":1", request, response));
        } else {
            assertSame(
                    failure,
                    assertThrows(
                            UncategorizedSQLException.class,
                            () ->
                                    controller.delivery(
                                            EXECUTION, EXECUTION + ":1", request, response)));
            request.setAttribute(RequestDispatcher.ERROR_STATUS_CODE, 500);
            errors.error(request, response);
        }
        verify(diagnostics).failed(DbDiagnostics.Phase.DELIVERY, failure, EXECUTION);
        return response;
    }

    private void assertError(MockHttpServletResponse response, int status, String disposition)
            throws Exception {
        assertEquals(status, response.getStatus());
        var envelope = JSON.readTree(response.getContentAsByteArray());
        assertEquals("test-request", envelope.path("request_id").asText());
        assertEquals("DEPENDENCY_UNAVAILABLE", envelope.path("payload").path("code").asText());
        assertEquals(disposition, envelope.path("payload").path("retry_disposition").asText());
    }

    @ParameterizedTest
    @ValueSource(
            strings = {"CARD_BATCH", "SCREENINGS", "ORDER_UPDATE", "SUPPLY_STATUS", "LONG_REVIEW"})
    void singleTransactionDeliveryTimeoutAllowsOriginalReplay(String kind) throws Exception {
        assertError(delivery(kind, "55P03", true), 503, "READ_STATUS_OR_SAFE_RETRY");
    }

    @Test
    void reportContentionKeepsOldMapping() throws Exception {
        assertError(delivery("ROUND_REPORT", "55P03", false), 500, "STOP");
    }

    @Test
    void statementCancellationKeepsOldMapping() throws Exception {
        assertError(delivery("CARD_BATCH", "57014", false), 500, "STOP");
    }

    @Test
    void usageTimeoutAllowsOriginalReplayAndRecordsOneDiagnostic() throws Exception {
        var invoice = EXECUTION + ":inv:1";
        var receipt = invoice + ":usage";
        var request =
                request(
                        "UsageReceipt",
                        "{\"invocation_id\":\""
                                + invoice
                                + "\",\"receipt_id\":\""
                                + receipt
                                + "\"}");
        var response = new MockHttpServletResponse();
        var failure = sqlFailure("55P03");
        when(budget.settle(eq(EXECUTION), anyString())).thenThrow(failure);
        assertNull(controller.usage(invoice, receipt, request, response));
        assertError(response, 503, "READ_STATUS_OR_SAFE_RETRY");
        verify(diagnostics).failed(DbDiagnostics.Phase.USAGE, failure, EXECUTION);
    }

    @Test
    void authorizationContentionDoesNotSignalReplay() throws Exception {
        var request = request("InvocationGrants", "{\"items\":[]}");
        var response = new MockHttpServletResponse();
        var failure = sqlFailure("55P03");
        when(tx.write(any())).thenThrow(failure);
        assertSame(
                failure,
                assertThrows(
                        UncategorizedSQLException.class,
                        () -> controller.grants(EXECUTION, request)));
        verifyNoInteractions(budget);
        request.setAttribute(RequestDispatcher.ERROR_STATUS_CODE, 500);
        errors.error(request, response);
        assertError(response, 500, "STOP");
    }
}
