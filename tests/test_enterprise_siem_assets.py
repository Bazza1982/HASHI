from __future__ import annotations

import configparser
import json
from pathlib import Path
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
SIEM_DIR = ROOT / "deploy" / "siem"


def test_siem_field_mapping_is_machine_readable():
    mapping = json.loads((SIEM_DIR / "hashi-audit-field-mapping.json").read_text(encoding="utf-8"))

    assert mapping["version"] == 1
    assert {"siem", "elastic-bulk", "splunk-hec", "otel"}.issubset(set(mapping["formats"]))
    for field in [
        "@timestamp",
        "event.id",
        "organization.id",
        "labels.hashi.audit.chain_index",
        "labels.hashi.audit.event_hash",
    ]:
        assert field in mapping["required_fields"]
    assert any(item["splunk_field"] == "hashi_event_hash" for item in mapping["field_mappings"])


def test_splunk_saved_searches_define_disabled_starter_alerts():
    parser = configparser.ConfigParser()
    parser.optionxform = str
    parser.read(SIEM_DIR / "splunk" / "savedsearches.conf", encoding="utf-8")

    expected_sections = {
        "HASHI Audit - Policy Deny Spike",
        "HASHI Audit - High Risk Egress Approval Required",
        "HASHI Audit - Chain Index Gap",
        "HASHI Audit - Exporter Stale",
    }
    assert expected_sections.issubset(set(parser.sections()))
    for section in expected_sections:
        assert parser[section]["disabled"] == "1"
        assert "index=hashi" in parser[section]["search"]
        assert "cron_schedule" in parser[section]


def test_splunk_dashboard_xml_is_valid():
    dashboard = ET.parse(SIEM_DIR / "splunk" / "dashboard.xml")

    root = dashboard.getroot()
    assert root.tag == "form"
    assert root.find("label").text == "HASHI Enterprise Audit Overview"
    assert "hashi.enterprise.audit" in (SIEM_DIR / "splunk" / "dashboard.xml").read_text(encoding="utf-8")


def test_elastic_assets_are_valid_json_lines():
    template = json.loads((SIEM_DIR / "elastic" / "hashi-audit-index-template.json").read_text(encoding="utf-8"))
    assert "hashi-audit-*" in template["index_patterns"]
    assert template["template"]["mappings"]["properties"]["event"]["properties"]["id"]["type"] == "keyword"

    rules = [
        json.loads(line)
        for line in (SIEM_DIR / "elastic" / "kibana-detection-rules.ndjson")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert len(rules) >= 3
    assert all(rule["type"] == "security-rule" for rule in rules)
    assert all(rule["attributes"]["enabled"] is False for rule in rules)


def test_otel_routing_example_mentions_hashi_audit_pipeline():
    text = (SIEM_DIR / "otel" / "otel-collector-routing.example.yaml").read_text(encoding="utf-8")

    assert "logs/hashi_audit" in text
    assert "event.dataset" in text
    assert "hashi.enterprise.audit" in text
    assert "HASHI_AUDIT_EXPORT_FORWARD_TOKEN" in text

