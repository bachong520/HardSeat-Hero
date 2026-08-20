import json

from app.domain.models import TrainSegment


def serialize_train_segments(segments: list[TrainSegment]) -> str:
    return json.dumps([segment.model_dump(mode="json") for segment in segments], ensure_ascii=False)


def deserialize_train_segments(value: str) -> list[TrainSegment]:
    payload = json.loads(value)
    if not isinstance(payload, list):
        raise ValueError("cached train segments must be a list")
    return [TrainSegment.model_validate(item) for item in payload]