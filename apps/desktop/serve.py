"""Start the product peer, using the repo's test adapter only for a synthetic bundle."""

import os
from pathlib import Path
from types import SimpleNamespace

if os.environ["DESKTOP_SYNTHETIC"] == "1":
    from auralisy_intelligence.agents import __main__ as service
    from auralisy_intelligence.providers import ToolGateway
    from java_integration_peer import Fixtures

    root = Path("/tmp")
    (root / "faults.json").write_text("{}", encoding="utf-8")
    fixture = Fixtures(root)
    service.OpenAIResponsesAdapter = lambda _: fixture
    service.ToolGateway = lambda *args, **kwargs: ToolGateway(fixture, **kwargs)
    service.serve(
        SimpleNamespace(config="/config/agent-service.json", port=8765, managed_stdin=False)
    )
else:
    os.execvp(
        "python",
        [
            "python",
            "-m",
            "auralisy_intelligence.agents",
            "serve",
            "--config",
            "/config/agent-service.json",
        ],
    )
