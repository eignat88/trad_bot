"""#176 final readiness check from bounded historical research artifacts.

Read-only, fail-closed. Does not authorize OOS or production deployment.
"""
import argparse
import json
from pathlib import Path

def inspect_pair(symbol, resumed):
    episodes=resumed["episodes"]
    clusters=resumed["clusters"]
    emissions=episodes["emissions"]
    ids=[e["event_id"] for e in emissions]
    if len(ids)!=len(set(ids)):
        raise ValueError(f"{symbol}: duplicate emitted event ID")
    if episodes["integrity"]["violations"]!=0 or episodes["integrity"]["result"]!="PASS":
        raise ValueError(f"{symbol}: event integrity failure")
    if set(ids)!=set(clusters["assignments"]):
        raise ValueError(f"{symbol}: cluster assignment/event ID mismatch")
    impact=episodes["pending_impact"]
    loss=impact["eligibility_loss_with_pending"]
    after=impact["confirmations_after_eligibility_loss"]
    loss_keys={(e["episode_id"],e["level_id"]) for e in loss}
    for e in after:
        if (e["event_id"],e["level_id"]) not in loss_keys:
            raise ValueError(f"{symbol}: confirmation impact without recorded loss")
    return {"symbol":symbol,"emissions":len(ids),"clusters":len(clusters["clusters"]),
        "eligibility_changes":impact["eligibility_changes"],
        "loss_with_pending_records":len(loss),
        "unique_impacted_episode_ids":len({x["episode_id"] for x in loss}),
        "post_loss_confirmation_records":len(after),
        "post_loss_unique_event_ids":len({x["event_id"] for x in after}),
        "pending_at_end":episodes["pending_at_end"],
        "historical_integrity":"PASS"}

def report(paths):
    results=[]
    for symbol,path in paths.items():
        results.append(inspect_pair(symbol,json.loads(Path(path).read_text())))
    return {"issue":176,"read_only":True,"results":results,
        "gates":{
            "historical_episode_id_parity":"PASS_PREVIOUS_VPS_37_OF_37",
            "historical_combined_restart":"PASS_PREVIOUS_VPS_BTC_ETH",
            "historical_atomic_interrupted_write":"PASS_PREVIOUS_VPS_BTC_ETH",
            "level_retirement_authorization":"BLOCKED_NO_PROVEN_SAFE_RULE",
            "producer_consumer_atomic_outbox":"NOT_IMPLEMENTED",
            "multi_generation_corrupt_checkpoint_recovery":"NOT_IMPLEMENTED",
            "prospective_level_selector_freeze":"NOT_APPROVED",
            "independent_execution_edge":"NOT_PROVEN",
            "deployment_readiness":"NO_GO"},
        "decision":"PHASE_2_NO_GO",
        "interpretation":"Historical research validation only; no new prospective OOS approved."}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--btc",type=Path,required=True)
    p.add_argument("--eth",type=Path,required=True)
    p.add_argument("--out",type=Path,required=True)
    args=p.parse_args()
    result=report({"BTCUSDT":args.btc,"ETHUSDT":args.eth})
    args.out.write_text(json.dumps(result,sort_keys=True,indent=2))
    for item in result["results"]:
        print(item["symbol"],"CONFIRMATIONS",item["emissions"],
              "POST_LOSS_UNIQUE",item["post_loss_unique_event_ids"],
              "LOSS_UNIQUE",item["unique_impacted_episode_ids"],
              "INTEGRITY",item["historical_integrity"])
    print("PHASE_2_NO_GO","SAVED",args.out)

if __name__=="__main__":
    main()
