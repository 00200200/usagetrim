from __future__ import annotations

import base64
import json

from usagetrim.core.json_slimmer import (
    filter_har_log,
    filter_rest_response,
    slim_json,
    slim_json_data,
)
from usagetrim.filters.rest import (
    filter_har_log as facade_filter_har_log,
)
from usagetrim.filters.rest import (
    filter_rest_response as facade_filter_rest_response,
)


def test_slim_json_data_array():
    data = {"users": [{"id": i, "name": f"User {i}"} for i in range(50)]}
    slimmed = slim_json_data(data, max_array_items=2)
    assert len(slimmed["users"]) == 3  # 2 items + 1 omitted notice
    assert "omitted by usagetrim" in slimmed["users"][2]
    assert "48 more items with identical schema omitted by usagetrim" in slimmed["users"][2]


def test_slim_json_data_similar_schema():
    data = [
        {"id": 1, "name": "Alice", "role": "admin"},
        {"id": 2, "name": "Bob", "email": "bob@example.com"},
        {"id": 3, "name": "Charlie"},
        {"id": 4, "name": "David"},
    ]
    slimmed = slim_json_data(data, max_array_items=2)
    assert len(slimmed) == 3
    assert "2 more items with similar schema omitted by usagetrim" in slimmed[2]


def test_slim_json_data_long_string():
    data = {"payload": "A" * 500}
    slimmed = slim_json_data(data, max_string_len=50)
    assert len(slimmed["payload"]) < 100
    assert "chars omitted" in slimmed["payload"]


def test_slim_json_data_base64_data_uri():
    fake_img_bytes = b"PNG_TEST_IMAGE_BYTES_FOR_AVATAR" * 50
    b64_str = base64.b64encode(fake_img_bytes).decode("ascii")
    data = {"avatar": f"data:image/png;base64,{b64_str}"}
    slimmed = slim_json_data(data)
    assert "[base64 payload" in slimmed["avatar"]
    assert "omitted]" in slimmed["avatar"]
    assert slimmed["avatar"].startswith("data:image/png;base64,")


def test_slim_json_data_raw_base64():
    raw_b64 = "MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQC0e3gO" * 4
    data = {"key_payload": raw_b64}
    slimmed = slim_json_data(data)
    assert "[base64 payload" in slimmed["key_payload"]
    assert "omitted]" in slimmed["key_payload"]


def test_slim_json_data_hateoas_links():
    data = {
        "id": 101,
        "_links": {
            "self": {"href": "/api/users/101"},
            "profile": {"href": "/api/profiles/101"},
            "orders": {"href": "/api/users/101/orders"},
            "settings": {"href": "/api/users/101/settings"},
            "avatar": {"href": "/api/users/101/avatar"},
        },
    }
    slimmed = slim_json_data(data)
    assert "self" in slimmed["_links"]
    assert "..." in slimmed["_links"]
    assert "more HATEOAS links omitted" in slimmed["_links"]["..."]


def test_slim_json_text_compaction():
    raw = json.dumps([{"id": i, "title": f"Task {i}"} for i in range(100)])
    result = slim_json(raw, max_array_items=3)
    assert "Task 0" in result
    assert "Task 1" in result
    assert "Task 2" in result
    assert "omitted by usagetrim" in result
    # CCR cache tag should be injected if substantial reduction
    assert "Ref: tc_" in result


def test_slim_json_invalid_fallback():
    invalid = "not json at all"
    assert slim_json(invalid) == invalid


def test_filter_rest_response_array_sampling_token_savings():
    users = [
        {
            "id": i,
            "username": f"user_{i}",
            "email": f"user_{i}@company.org",
            "department": "Engineering",
            "active": True,
        }
        for i in range(100)
    ]
    raw_payload = json.dumps(users, indent=2)
    compacted = filter_rest_response(raw_payload)

    # Acceptance criteria: > 85% token / length reduction
    reduction = (len(raw_payload) - len(compacted)) / len(raw_payload)
    assert reduction > 0.85
    assert "user_0" in compacted
    assert "user_1" in compacted
    assert "identical schema omitted by usagetrim" in compacted
    assert "Ref: tc_" in compacted


def test_filter_rest_response_http_headers_and_json_body():
    http_headers = (
        "HTTP/1.1 200 OK\r\n"
        "Date: Thu, 08 Oct 2026 12:00:00 GMT\r\n"
        "Server: nginx/1.24.0\r\n"
        'ETag: W/"123456789"\r\n'
        "Content-Type: application/json\r\n"
        "X-Process-Time: 12ms\r\n"
        "\r\n"
    )
    items = [{"id": i, "sku": f"SKU-{i:04d}", "price": 9.99} for i in range(50)]
    raw_response = http_headers + json.dumps(items, indent=2)

    compacted = filter_rest_response(raw_response)
    assert "HTTP/1.1 200 OK" in compacted
    assert "Content-Type: application/json" in compacted
    assert "routine response headers collapsed" in compacted
    assert "SKU-0000" in compacted
    assert "identical schema omitted by usagetrim" in compacted
    assert "Ref: tc_" in compacted


def test_filter_har_log_compactor():
    har_data = {
        "log": {
            "version": "1.2",
            "creator": {"name": "DevTools", "version": "130.0"},
            "entries": [
                {
                    "startedDateTime": "2026-10-08T12:00:00.000Z",
                    "time": 35,
                    "request": {
                        "method": "GET",
                        "url": "https://api.example.com/v1/products",
                        "headers": [
                            {"name": "Accept", "value": "application/json"},
                            {"name": "User-Agent", "value": "curl/8.4.0"},
                        ],
                    },
                    "response": {
                        "status": 200,
                        "statusText": "OK",
                        "content": {
                            "size": 5000,
                            "mimeType": "application/json",
                            "text": json.dumps([{"id": i, "name": f"P-{i}"} for i in range(40)]),
                        },
                    },
                },
                {
                    "startedDateTime": "2026-10-08T12:00:01.000Z",
                    "time": 20,
                    "request": {
                        "method": "GET",
                        "url": "https://api.example.com/v1/categories",
                        "headers": [],
                    },
                    "response": {
                        "status": 200,
                        "statusText": "OK",
                        "content": {
                            "size": 300,
                            "mimeType": "application/json",
                            "text": json.dumps([{"id": 1, "name": "Electronics"}]),
                        },
                    },
                },
            ],
        }
    }
    raw_har = json.dumps(har_data, indent=2)
    compacted = filter_har_log(raw_har)

    assert "https://api.example.com/v1/products" in compacted
    assert "GET" in compacted
    assert "200" in compacted
    # Check facade export
    assert facade_filter_har_log(raw_har) == compacted
    assert facade_filter_rest_response(raw_har) == compacted
