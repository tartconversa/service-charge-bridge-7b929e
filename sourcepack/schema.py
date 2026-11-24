"""来源表字段契约，供入仓时复用与检查器一致的类型规则。"""

TABLES = {
    "packages": {
        "key": ["package_id"],
        "columns": [
            {"name": "package_id", "type": "string"},
            {"name": "person", "type": "string"},
            {"name": "sold_at", "type": "datetime"},
            {"name": "price", "type": "number"},
        ],
    },
    "slots": {
        "key": ["slot_id"],
        "columns": [
            {"name": "slot_id", "type": "string"},
            {"name": "package_id", "type": "string"},
            {"name": "service_code", "type": "string"},
            {"name": "units", "type": "number"},
            {"name": "relative_price", "type": "number"},
            {"name": "exclusive_group", "type": "string", "nullable": True},
        ],
    },
    "requests": {
        "key": ["request_id"],
        "columns": [
            {"name": "request_id", "type": "string"},
            {"name": "person", "type": "string"},
            {"name": "service_code", "type": "string"},
            {"name": "state", "type": "string"},
        ],
    },
    "executions": {
        "key": ["execution_id"],
        "columns": [
            {"name": "execution_id", "type": "string"},
            {"name": "request_id", "type": "string"},
            {"name": "person", "type": "string"},
            {"name": "service_code", "type": "string"},
            {"name": "team", "type": "string"},
            {"name": "units", "type": "number"},
            {"name": "at", "type": "datetime"},
            {"name": "status", "type": "string"},
        ],
    },
    "fulfillment_links": {
        "key": ["link_id"],
        "columns": [
            {"name": "link_id", "type": "string"},
            {"name": "execution_id", "type": "string"},
            {"name": "slot_id", "type": "string"},
            {"name": "units", "type": "number"},
        ],
    },
    "substitutions": {
        "key": ["rule_id"],
        "columns": [
            {"name": "rule_id", "type": "string"},
            {"name": "slot_service", "type": "string"},
            {"name": "actual_service", "type": "string"},
            {"name": "unit_ratio", "type": "number"},
        ],
    },
    "refunds": {
        "key": ["refund_id"],
        "columns": [
            {"name": "refund_id", "type": "string"},
            {"name": "slot_id", "type": "string"},
            {"name": "units", "type": "number"},
            {"name": "amount", "type": "number"},
            {"name": "at", "type": "datetime"},
        ],
    },
    "cash": {
        "key": ["cash_id"],
        "columns": [
            {"name": "cash_id", "type": "string"},
            {"name": "package_id", "type": "string"},
            {"name": "kind", "type": "string"},
            {"name": "amount", "type": "number"},
            {"name": "at", "type": "datetime"},
        ],
    },
}

# 事实层允许的取值域，超出取值域不静默吞掉。
DOMAINS = {
    "requests.state": {"requested", "cancelled"},
    "executions.status": {"confirmed", "pending"},
    "cash.kind": {"receive", "refund"},
}
