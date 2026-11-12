from pathlib import Path

import pytest

from solution.schema import load_dispositions, load_workflows

SCHEMAS = Path(__file__).resolve().parents[2] / "assignment-a-data" / "schemas"


@pytest.fixture(scope="module")
def workflows():
    return load_workflows(SCHEMAS)


def test_loads_both_workflows(workflows):
    assert set(workflows) == {"motor_renewal", "policy_feedback"}


def test_reference_and_pii_fields(workflows):
    mr = workflows["motor_renewal"]
    assert mr.reference_field.name == "policy_number"
    assert [f.name for f in mr.pii_fields] == ["customer_name", "phone"]
    assert workflows["policy_feedback"].reference_field.name == "order_id"


@pytest.mark.parametrize("header, field", [
    ("Mobile No", "phone"), ("MOBILE_NO", "phone"), ("Insured Name", "customer_name"),
    ("customer_name", "customer_name"), ("Policy No", "policy_number"), ("Expiry Date", "policy_expiry"),
    ("Premium", "premium_amount"), ("Language", "preferred_language"),
])
def test_header_matching_motor(workflows, header, field):
    assert workflows["motor_renewal"].match_header(header).name == field


def test_unmatched_header_ignored(workflows):
    assert workflows["motor_renewal"].match_header("agent_notes") is None


def test_header_matching_feedback(workflows):
    pf = workflows["policy_feedback"]
    assert pf.match_header("Order ID").name == "order_id"
    assert pf.match_header("Contact").name == "phone"
    assert pf.match_header("Item").name == "product"


def test_enum_default(workflows):
    lang = next(f for f in workflows["motor_renewal"].fields if f.name == "preferred_language")
    assert lang.default == "en" and "hi" in lang.values


def test_dispositions():
    d = {(x.workflow_id, x.code): x for x in load_dispositions(SCHEMAS / "dispositions.csv")}
    assert len(d) == 14
    assert d[("motor_renewal", "renewed")].is_terminal and d[("motor_renewal", "renewed")].is_success
    assert not d[("motor_renewal", "callback_requested")].is_terminal
    assert d[("policy_feedback", "refused")].is_terminal and not d[("policy_feedback", "refused")].is_success
