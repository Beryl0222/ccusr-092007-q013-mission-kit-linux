"""留置交接与凭证。

留置当地的资产必须完成双语交接：条款须覆盖清单声明的全部语言，
有接收方培训记录，且捐赠方与接收方分别确认责任后，交接才算完成。
交接、返运、销毁都会留下带内容指纹（digest）的凭证，供结存复算
与事后审计核对。
"""

from __future__ import annotations

import hashlib
import json

from .model import Certificate

# 交接双方：捐赠方与接收方都须确认责任。
REQUIRED_PARTIES = ("donor", "recipient")


def missing_languages(terms: dict, languages: tuple[str, ...]) -> list[str]:
    """条款缺少哪些语言的非空文本。"""

    return [lang for lang in languages if not str(terms.get(lang, "")).strip()]


def make_certificate(
    kind: str,
    certificate_id: str,
    item_ids,
    issuer: str,
    issued_at: str,
    detail: str = "",
) -> Certificate:
    """生成凭证并计算内容指纹；同样的内容永远得到同样的 digest。"""

    payload = {
        "certificate_id": certificate_id,
        "kind": kind,
        "item_ids": sorted(item_ids),
        "issuer": issuer,
        "issued_at": issued_at,
        "detail": detail,
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return Certificate(
        certificate_id=certificate_id,
        kind=kind,
        item_ids=tuple(sorted(item_ids)),
        issuer=issuer,
        issued_at=issued_at,
        detail=detail,
        digest=digest,
    )
