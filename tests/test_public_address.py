from __future__ import annotations

import pytest

from orchestrator.hchat_delivery import validate_hchat_target_format
from remote.internet_address import (
    AddressError,
    ExchangeAddress,
    LocalAddress,
    PublicAddress,
    parse_hchat_address,
)
from tools.hchat_send import parse_hchat_message


def test_public_address_is_strict_and_canonical():
    address = parse_hchat_address("Planner@Home.BarryTianLi")

    assert isinstance(address, PublicAddress)
    assert address.agent == "planner"
    assert address.instance_alias == "home"
    assert address.username == "barrytianli"
    assert address.canonical == "planner@home.barrytianli"


def test_local_address_contract_is_unchanged():
    address = parse_hchat_address("planner@HASHI_2")

    assert isinstance(address, LocalAddress)
    assert address.agent == "planner"
    assert address.instance_id == "HASHI_2"
    assert address.canonical == "planner@HASHI_2"


@pytest.mark.parametrize(
    "value",
    [
        "planner@home.bad.user",
        "planner@home.ab",
        "planner@home.barry!",
        "planner@@home.barry",
        "planner@home_.barry",
    ],
)
def test_failed_dotted_public_address_never_downgrades_to_lan(value):
    with pytest.raises(AddressError):
        parse_hchat_address(value)


def test_same_alias_for_different_actors_remains_distinct():
    alice = PublicAddress.parse("planner@home.alice")
    barry = PublicAddress.parse("planner@home.barry")

    assert alice.canonical != barry.canonical
    assert alice.instance_address != barry.instance_address


def test_exchange_address_requires_agent_address_binding():
    with pytest.raises(AddressError):
        ExchangeAddress.from_mapping(
            {
                "authority_id": "authority_1",
                "actor_id": "actor_1",
                "registered_instance_id": "instance_1",
                "agent_id": "planner",
                "address": "reviewer@home.barry",
            }
        )


def test_hchat_draft_and_display_parser_accept_public_address():
    assert (
        validate_hchat_target_format("planner@home.barry")
        == "planner@home.barry"
    )
    parsed = parse_hchat_message(
        "[hchat from planner@home.barry] hello"
    )
    assert parsed == {
        "agent": "planner",
        "instance_id": "HOME.BARRY",
        "body": "hello",
    }
