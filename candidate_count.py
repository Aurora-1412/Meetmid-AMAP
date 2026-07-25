CANDIDATE_COUNT_DEFAULT = 10
CANDIDATE_COUNT_MIN = 1
CANDIDATE_COUNT_MAX = 20
CANDIDATE_COUNT_ERROR = "候选点数量必须是 1 到 20 之间的整数"


def parse_candidate_count(value: object = None) -> int:
    if value is None:
        return CANDIDATE_COUNT_DEFAULT
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not CANDIDATE_COUNT_MIN <= value <= CANDIDATE_COUNT_MAX
    ):
        raise ValueError(CANDIDATE_COUNT_ERROR)
    return value
