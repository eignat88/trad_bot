"""#176 root-cause matching of unequal episode IDs without altering replay semantics."""
from collections import Counter, defaultdict

def compare_events(selected, full):
    a={e["event_id"]: e for e in selected}
    b={e["event_id"]: e for e in full}
    if len(a)!=len(selected) or len(b)!=len(full):
        raise ValueError("duplicate event IDs")
    only_a=[a[k] for k in sorted(a.keys()-b.keys())]
    only_b=[b[k] for k in sorted(b.keys()-a.keys())]
    def signature(e):
        return (e["level_id"],e["known_at_ms"],e["level_price_at_touch"])
    a_by=defaultdict(list); b_by=defaultdict(list)
    for e in only_a:a_by[signature(e)].append(e)
    for e in only_b:b_by[signature(e)].append(e)
    matched=[]
    for key in sorted(a_by.keys() & b_by.keys()):
        left=sorted(a_by[key],key=lambda e:(e["first_touch_open_ms"],e["event_id"]))
        right=sorted(b_by[key],key=lambda e:(e["first_touch_open_ms"],e["event_id"]))
        for aa,bb in zip(left,right):
            matched.append({"level_id":key[0],"known_at_ms":key[1],
                "level_price_at_touch":key[2],
                "selected_event_id":aa["event_id"],"full_event_id":bb["event_id"],
                "selected_first_touch_open_ms":aa["first_touch_open_ms"],
                "full_first_touch_open_ms":bb["first_touch_open_ms"],
                "touch_shift_ms":bb["first_touch_open_ms"]-aa["first_touch_open_ms"],
                "classification":"SAME_LEVEL_CONFIRMATION_DIFFERENT_EPISODE_ID"})
    matched_a={m["selected_event_id"] for m in matched}
    matched_b={m["full_event_id"] for m in matched}
    return {"selected_count":len(a),"full_count":len(b),"common_ids":len(a.keys() & b.keys()),
        "selected_only":len(only_a),"full_only":len(only_b),
        "matched_same_level_confirmation":len(matched),
        "matches":matched,
        "unmatched_selected":[e for e in only_a if e["event_id"] not in matched_a],
        "unmatched_full":[e for e in only_b if e["event_id"] not in matched_b],
        "verdict":"EPISODE_ID_DIAGNOSTIC_NOT_OOS"}
