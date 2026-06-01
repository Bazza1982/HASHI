from remote.delivery_results import classify_delivery_result, format_delivery_result


def test_classify_protocol_error_codes_to_operator_results():
    assert classify_delivery_result({"body": {"code": "target_agent_not_found"}}) == "target_not_found"
    assert classify_delivery_result({"body": {"code": "target_instance_not_found"}}) == "target_instance_offline"
    assert classify_delivery_result({"body": {"code": "forward_failed"}}) == "target_instance_offline"
    assert classify_delivery_result({"body": {"code": "local_enqueue_failed"}}) == "target_agent_unavailable"
    assert classify_delivery_result({"body": {"code": "auth_failed"}}) == "auth_failed"
    assert classify_delivery_result({"body": {"code": "delivery_expired"}}) == "delivery_rejected"
    assert classify_delivery_result({"body": {"code": "reply_timeout"}}) == "delivery_timed_out"


def test_format_delivery_result_includes_operator_class_and_detail():
    text = format_delivery_result(
        {
            "message_type": "error",
            "body": {
                "code": "target_agent_not_found",
                "message": "Target agent 'mimi' not found",
            },
        }
    )

    assert text.startswith("target_not_found:")
    assert "Target agent 'mimi' not found" in text


def test_transport_errors_map_to_operator_results():
    assert classify_delivery_result(transport_error="HTTP Error 401: Unauthorized") == "auth_failed"
    assert classify_delivery_result(transport_error="timed out") == "delivery_timed_out"
    assert classify_delivery_result(transport_error="connection refused") == "target_instance_offline"
