"""Maker-checker handlers for decision-logic changes (rules and thresholds).

A rule create/update/disable (``RULE_CHANGE``) or a policy threshold change
(``POLICY_THRESHOLDS``) is only a *request* until a second, sufficiently
senior user approves it; these handlers apply the approved change to the
database and the live engine and write it to the audit chain.
"""

from __future__ import annotations

from typing import Any

from app.cases.service import CaseError
from app.db.audit import AuditEntry
from app.scoring import rule_store
from app.scoring.policy import PolicyError
from app.scoring.rules import RuleDef, RuleError

RULE_OPS = ("create", "update", "disable")


class DecisionLogicGovernance:
    def __init__(self, db: Any, engine: Any, writer: Any) -> None:
        self.db = db
        self.engine = engine
        self.writer = writer

    async def reload_rules(self) -> str:
        async with self.db.session() as session:
            definitions = await rule_store.load_rules(session)
        ruleset = rule_store.build_ruleset(definitions)
        self.engine.set_ruleset(ruleset)
        return str(ruleset.version)

    async def _audit(
        self,
        event: str,
        entity: str,
        approval: dict[str, Any],
        actor: str,
        reason: str,
        **payload: Any,
    ) -> None:
        await self.writer.record_audit(
            AuditEntry(
                event_type=event,
                entity_type="rule" if event.startswith("RULE") else "policy",
                entity_id=entity,
                actor=str(approval["requested_by"]),
                reason=reason,
                payload={**payload, "approval_id": approval["id"], "approved_by": actor},
            )
        )

    async def apply_rule_change(self, approval: dict[str, Any], actor: str) -> dict[str, Any]:
        payload = approval["payload"]
        op = payload.get("op")
        if op not in RULE_OPS:
            raise CaseError(f"geçersiz kural işlemi: {op}")
        try:
            definition = RuleDef.from_dict(payload["definition"])
            definition.validate()
        except (RuleError, KeyError, TypeError) as exc:
            raise CaseError(f"kural geçersiz: {exc}") from None
        async with self.db.transaction() as session:
            current = await rule_store.get_rule(session, definition.id)
            if op == "create" and current is not None:
                raise CaseError("Bu kimlikte bir kural zaten var")
            if op != "create":
                if current is None:
                    raise CaseError("Kural bulunamadı")
                if current.version != payload.get("base_version"):
                    raise CaseError("kural talepten sonra değişti — yeni bir talep oluşturun")
            if op == "disable" and current is not None:
                definition = RuleDef.from_dict({**current.to_dict(), "enabled": False})
            stored = await rule_store.save_rule(session, definition, str(approval["requested_by"]))
        version = await self.reload_rules()
        event = {"create": "RULE_CREATE", "update": "RULE_UPDATE", "disable": "RULE_DISABLE"}[op]
        verb = {"create": "oluşturuldu", "update": "güncellendi", "disable": "devre dışı"}[op]
        await self._audit(
            event,
            stored.id,
            approval,
            actor,
            f"Kural {verb}: {stored.id} v{stored.version}",
            definition=stored.to_dict(),
            ruleset_version=version,
        )
        return {"rule": stored.to_dict(), "ruleset_version": version}

    async def apply_thresholds(self, approval: dict[str, Any], actor: str) -> dict[str, Any]:
        payload = approval["payload"]
        old = self.engine.policy.thresholds.as_dict()
        try:
            new = self.engine.policy.set_thresholds(
                float(payload["step_up"]), float(payload["hold"]), float(payload["block"])
            )
        except (PolicyError, KeyError, TypeError, ValueError) as exc:
            raise CaseError(f"eşikler geçersiz: {exc}") from None
        await self._audit(
            "POLICY_THRESHOLDS",
            "thresholds",
            approval,
            actor,
            "Politika eşikleri değiştirildi",
            old=old,
            new=new.as_dict(),
        )
        return {"thresholds": new.as_dict()}
