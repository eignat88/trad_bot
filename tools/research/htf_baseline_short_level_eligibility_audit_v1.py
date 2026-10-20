"""#176 level eligibility transitions; diagnostic only, never cancels pending."""
from collections import Counter

class LevelEligibilityAudit:
    def __init__(self, *, record_transitions=True):
        self.ever_seen={}
        self.last_eligible=set()
        self.transitions=[]
        self.counts=Counter()
        self.record_transitions=record_transitions

    def advance(self,asof_ms,eligible_levels,reference_price,pending_ids=()):
        current={x.level_id:x for x in eligible_levels}
        pending=set(pending_ids)
        for lid,level in current.items():
            if lid not in self.ever_seen:
                self.ever_seen[lid]=level
                if self.record_transitions:
                    self.counts["DISCOVERED"]+=1
            elif lid not in self.last_eligible:
                if self.record_transitions:
                    self.counts["RE_ELIGIBLE"]+=1
                    self.transitions.append({"asof_ms":asof_ms,"level_id":lid,"state":"RE_ELIGIBLE",
                                             "pending":lid in pending})
        if self.record_transitions:
            for lid in sorted(self.last_eligible-current.keys()):
                level=self.ever_seen[lid]
                reason="BELOW_OR_AT_REFERENCE" if level.price<=reference_price else "NOT_IN_CURRENT_ELIGIBLE_SET"
                self.transitions.append({"asof_ms":asof_ms,"level_id":lid,
                   "state":"NOT_CURRENTLY_ELIGIBLE","reason":reason,"pending":lid in pending})
                self.counts[reason]+=1
        self.last_eligible=set(current)
        return self.transitions

    def result(self):
        return {"counts":dict(self.counts),"transitions":self.transitions,
                "levels_ever_seen":len(self.ever_seen),
                "verdict":"ELIGIBILITY_LOSS_IS_NOT_PROVEN_RETIREMENT"}
