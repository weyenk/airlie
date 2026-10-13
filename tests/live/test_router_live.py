"""Live: Kev answers a closed-set question with probabilities over exactly the offered options."""

import pytest

from airlie.config import load_config
from airlie.router import jev_client

pytestmark = pytest.mark.live


async def test_kev_routes_a_test_task_to_qa() -> None:
    cfg = load_config()
    client = jev_client(cfg)
    criteria = {
        "coder": "Implement or change code",
        "qa": "Write tests, edge cases, regression plans",
        "docs": "README and guides",
    }
    ans = await client.ask(
        "Add edge-case tests for login rate limiting",
        "role",
        "Which specialist should handle this task?",
        criteria,
    )
    assert set(ans.probabilities) == set(criteria)
    assert ans.choice == "qa" and ans.confidence > 0.5
    await client.aclose()
