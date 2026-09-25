"""Maker-checker handlers for decision-logic changes (rules and thresholds).

A rule create/update/disable (``RULE_CHANGE``) or a policy threshold change
(``POLICY_THRESHOLDS``) is only a *request* until a second, sufficiently
senior user approves it; these handlers apply the approved change to the
database and the live engine and write it to the audit chain.

A5: each handler runs inside the approval's transaction
(:class:`app.cases.service.ApprovalTx`): the rule row, the ``runtime_config``
publish that tells the other workers, the approval status flip and the audit
entries commit together; the local engine is only swapped after the commit.
A failure (invalid change, concurrent config change, database error) rolls
everything back and leaves the request pending.
"""

from __future__ import annotations

from typing import Any

from app.cases.service import ApprovalTx, CaseError
from app.db.audit import AuditEntry
from app.scoring import rule_store
from app.scoring.policy import PolicyError, Thresholds
from app.scoring.rules import RuleDef, RuleError

RULE_OPS = ("create", "update", "disable")


class DecisionLogicGovernance:
    def __init__(self, db: Any, engine: Any, writer: Any, *, config: Any = None) -> None:
        self.db = db
        self.engine = engine
        self.writer = writer
        # H3: ConfigSync — the change is published to the other workers
        self.config = config

    async def _publish(self, tx: ApprovalTx, key: str, value: dict[str, Any], actor: str) -> None:
        if self.config is None:
            return
        config = self.config
        version = await config.publish_in(tx.session, key, value, actor=actor)
        tx.on_commit(lambda: config.mark_seen(key, version))

    @staticmethod
    def _audit(
        tx: ApprovalTx,
        event: str,
        entity: str,
        approval: dict[str, Any],
        actor: str,
        reason: str,
        **payload: Any,
    ) -> None:
        tx.audits.append(
            AuditEntry(
                event_type=event,
                entity_type="rule" if event.startswith("RULE") else "policy",
                entity_id=entity,
                actor=str(approval["requested_by"]),
                reason=reason,
                payload={**payload, "approval_id": approval["id"], "approved_by": actor},
            )
        )

    async def apply_rule_change(
        self, approval: dict[str, Any], actor: str, tx: ApprovalTx
    ) -> dict[str, Any]:
        payload = approval["payload"]
        op = payload.get("op")
        if op not in RULE_OPS:
            raise CaseError(f"geçersiz kural işlemi: {op}")
        try:
            definition = RuleDef.from_dict(payload["definition"])
            definition.validate()
        except (RuleError, KeyError, TypeError) as exc:
            raise CaseError(f"kural geçersiz: {exc}") from None
        session = tx.session
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
        requester = str(approval["requested_by"])
        stored = await rule_store.save_rule(session, definition, requester)
        await session.flush()
        ruleset = rule_store.build_ruleset(await rule_store.load_rules(session))
        version = str(ruleset.version)
        await self._publish(tx, "rules", {"version": version}, requester)
        tx.on_commit(lambda: self.engine.set_ruleset(ruleset))
        event = {"create": "RULE_CREATE", "update": "RULE_UPDATE", "disable": "RULE_DISABLE"}[op]
        verb = {"create": "oluşturuldu", "update": "güncellendi", "disable": "devre dışı"}[op]
        self._audit(
            tx,
            event,
            stored.id,
            approval,
            actor,
            f"Kural {verb}: {stored.id} v{stored.version}",
            definition=stored.to_dict(),
            ruleset_version=version,
        )
        return {"rule": stored.to_dict(), "ruleset_version": version}

    async def apply_thresholds(
        self, approval: dict[str, Any], actor: str, tx: ApprovalTx
    ) -> dict[str, Any]:
        payload = approval["payload"]
        old = self.engine.policy.thresholds.as_dict()
        try:
            new = Thresholds(
                float(payload["step_up"]), float(payload["hold"]), float(payload["block"])
            )
        except (PolicyError, KeyError, TypeError, ValueError) as exc:
            raise CaseError(f"eşikler geçersiz: {exc}") from None
        await self._publish(tx, "thresholds", new.as_dict(), str(approval["requested_by"]))
        tx.on_commit(lambda: self._set_local(new))
        self._audit(
            tx,
            "POLICY_THRESHOLDS",
            "thresholds",
            approval,
            actor,
            "Politika eşikleri değiştirildi",
            old=old,
            new=new.as_dict(),
        )
        return {"thresholds": new.as_dict()}

    def _set_local(self, new: Thresholds) -> None:
        self.engine.policy.set_thresholds(new.step_up, new.hold, new.block)
