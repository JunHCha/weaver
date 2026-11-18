"""Shared read and write classification of business facts."""

from typing import Literal

from weaver.schema import Thread


def presentation(thread: Thread) -> Literal["fact", "history"]:
    return (
        "history"
        if (
            thread.kind in ("태그정정", "실행", "거절")
            or thread.retracts is not None
            or thread.thread_id.startswith("wiki-field:")
            or (
                thread.thread_id.startswith("wiki:")
                and thread.field is not None
                and thread.content == f"{thread.field} 변경"
            )
        )
        else "fact"
    )
