"""#176 final readiness check from bounded historical research artifacts.

Read-only, fail-closed. Does not authorize OOS or production deployment.
"""
import argparse
import json
from pathlib import Path

def _required_mapping(value, field):
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _required_string(value, field):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _require_event(event, index):
    if not isinstance(event, dict):
        raise ValueError(f"emission {index} must be an object")
    return _required_string(event.get("event_id"), f"emission {index} event_id")


def _validate_cluster_integrity(symbol, emissions, clusters_payload):
    clusters = _required_mapping(clusters_payload.get("clusters"), "clusters.clusters")
    assignments = _required_mapping(clusters_payload.get("assignments"), "clusters.assignments")
    event_ids = [_require_event(event, index) for index, event in enumerate(emissions)]
    if len(event_ids) != len(set(event_ids)):
        raise ValueError(f"{symbol}: duplicate emitted event ID")
    if len(assignments) != len(event_ids):
        raise ValueError(f"{symbol}: cluster assignment/event ID mismatch")
    if set(event_ids) != set(assignments):
        raise ValueError(f"{symbol}: cluster assignment/event ID mismatch")

    cluster_members = {}
    for cluster_id, cluster in clusters.items():
        _required_string(cluster_id, "cluster ID")
        if not isinstance(cluster, dict):
            raise ValueError(f"{symbol}: cluster {cluster_id} must be an object")
        _required_string(cluster.get("reaction_cluster_id"), f"cluster {cluster_id} reaction_cluster_id")
        if cluster["reaction_cluster_id"] != cluster_id:
            raise ValueError(f"{symbol}: cluster key/ID mismatch")
        members = cluster.get("member_event_ids")
        if not isinstance(members, list):
            raise ValueError(f"{symbol}: cluster {cluster_id} member_event_ids must be a list")
        normalized_members = [_required_string(member, f"cluster {cluster_id} member_event_id")
                              for member in members]
        if len(normalized_members) != len(set(normalized_members)):
            raise ValueError(f"{symbol}: duplicate cluster member ID")
        if not normalized_members:
            raise ValueError(f"{symbol}: empty cluster membership")
        duplicated_members = set(cluster_members) & set(normalized_members)
        if duplicated_members:
            raise ValueError(f"{symbol}: duplicate cluster member ID")
        cluster_members.update({member: cluster_id for member in normalized_members})

    if set(cluster_members) != set(event_ids):
        raise ValueError(f"{symbol}: cluster membership/emission ID mismatch")

    assigned_clusters = set()
    for event_id in event_ids:
        cluster_id = assignments[event_id]
        if not isinstance(cluster_id, str) or not cluster_id:
            raise ValueError(f"{symbol}: invalid cluster assignment for {event_id}")
        if cluster_id not in clusters:
            raise ValueError(f"{symbol}: assignment references missing cluster {cluster_id}")
        if cluster_members.get(event_id) != cluster_id:
            raise ValueError(f"{symbol}: assignment/cluster membership mismatch")
        assigned_clusters.add(cluster_id)
    if assigned_clusters != set(clusters):
        raise ValueError(f"{symbol}: cluster without assigned emissions")


def inspect_pair(symbol, resumed):
    resumed=_required_mapping(resumed, "resumed artifact")
    episodes=_required_mapping(resumed.get("episodes"), "episodes")
    clusters_payload=_required_mapping(resumed.get("clusters"), "clusters")
    emissions=episodes.get("emissions")
    if not isinstance(emissions,list):
        raise ValueError(f"{symbol}: emissions must be a list")
    ids=[_require_event(e,index) for index,e in enumerate(emissions)]
    if len(ids)!=len(set(ids)):
        raise ValueError(f"{symbol}: duplicate emitted event ID")
    integrity=_required_mapping(episodes.get("integrity"), "episodes.integrity")
    if integrity.get("violations")!=0 or integrity.get("result")!="PASS":
        raise ValueError(f"{symbol}: event integrity failure")
    _validate_cluster_integrity(symbol, emissions, clusters_payload)
    impact=_required_mapping(episodes.get("pending_impact"), "episodes.pending_impact")
    loss=impact["eligibility_loss_with_pending"]
    after=impact["confirmations_after_eligibility_loss"]
    loss_keys={(e["episode_id"],e["level_id"]) for e in loss}
    for e in after:
        if (e["event_id"],e["level_id"]) not in loss_keys:
            raise ValueError(f"{symbol}: confirmation impact without recorded loss")
    return {"symbol":symbol,"emissions":len(ids),"clusters":len(clusters_payload["clusters"]),
        "eligibility_changes":impact["eligibility_changes"],
        "loss_with_pending_records":len(loss),
        "unique_impacted_episode_ids":len({x["episode_id"] for x in loss}),
        "post_loss_confirmation_records":len(after),
        "post_loss_unique_event_ids":len({x["event_id"] for x in after}),
        "eligibility_loss_records":len(episodes.get("eligibility_loss_records",[])),
        "pending_at_end":episodes["pending_at_end"],
        "historical_integrity":"PASS"}

def report(paths):
    results=[]
    for symbol,path in paths.items():
        results.append(inspect_pair(symbol,json.loads(Path(path).read_text())))
    return {"issue":176,"read_only":True,"results":results,
        "gates":{
            "local_regression":"PENDING_POST_REPAIR_RUN",
            "historical_episode_id_parity":"PASS_PREVIOUS_VPS_37_OF_37",
            "historical_combined_restart":"PASS_PREVIOUS_VPS_BTC_ETH",
            "historical_atomic_interrupted_write":"PASS_PREVIOUS_VPS_BTC_ETH",
            "level_retirement_authorization":"BLOCKED_NO_PROVEN_SAFE_RULE",
            "producer_consumer_atomic_outbox":"NOT_IMPLEMENTED",
            "multi_generation_corrupt_checkpoint_recovery":"NOT_IMPLEMENTED",
            "provenance_replay_recovery":"RESEARCH_SINGLE_FILE_NOT_MULTI_GENERATION",
            "checkpoint_writer_exclusivity":"RESEARCH_FILE_LOCK_EXCLUSIVE_WITH_OPERATOR_STALE_RECOVERY_ONLY",
            "prospective_level_selector_freeze":"NOT_APPROVED",
            "prospective_protocol_checklist":"PROPOSED_NOT_APPROVED",
            "unavailable_semantics_repair":"PENDING_VPS_AB",
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
