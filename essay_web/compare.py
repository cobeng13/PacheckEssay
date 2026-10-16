"""Offline instructor-scored comparison; performs no network requests."""
import argparse
import csv
import json
import math
import statistics
from decimal import Decimal
from pathlib import Path


def compare(scores_path, requests_path):
    with Path(scores_path).open(encoding='utf-8-sig', newline='') as stream:
        rows=list(csv.DictReader(stream))
    if len(rows)<30 or len({r['sample_id'] for r in rows}) != len(rows):
        raise ValueError('Use at least 30 unique sample IDs.')
    metrics={}
    for model in ('gpt-4.1-mini','gpt-6-luna'):
        errors=[]
        for row in rows:
            maximum,truth,prediction=(float(row[key]) for key in ('max_points','instructor_score',model))
            if not all(math.isfinite(v) for v in (maximum,truth,prediction)) or not 0<=truth<=maximum or not 0<=prediction<=maximum or maximum<=0:
                raise ValueError('Scores must be finite and within rubric maximum.')
            errors.append(abs(prediction-truth)/maximum)
        metrics[model]={'mean_absolute_error_fraction':statistics.mean(errors),'agreement_within_10_percent':sum(e<=0.1+1e-9 for e in errors)/len(errors)}
    with Path(requests_path).open(encoding='utf-8-sig',newline='') as stream:
        requests=list(csv.DictReader(stream))
    for model in metrics:
        attempts=[r for r in requests if r['model']==model]
        success=[r for r in attempts if r['status']=='success']
        known=bool(attempts) and all(r['cost_usd'] for r in attempts)
        total=sum((Decimal(r['cost_usd']) for r in attempts if r['cost_usd']),Decimal(0))
        metrics[model].update(known_cost_usd=str(total),unknown_costs=sum(not r['cost_usd'] for r in attempts),
            cost_per_success_usd=str(total/len(success)) if known and success else None,
            median_success_latency_seconds=statistics.median(float(r['duration_seconds']) for r in success) if success else None)
    baseline,luna=metrics['gpt-4.1-mini'],metrics['gpt-6-luna']
    cost_ok=bool(baseline['cost_per_success_usd'] and luna['cost_per_success_usd']) and Decimal(luna['cost_per_success_usd'])<Decimal(baseline['cost_per_success_usd'])
    return {'samples':len(rows),'models':metrics,'numeric_gate_pass':bool(cost_ok and luna['agreement_within_10_percent']>=0.9 and luna['mean_absolute_error_fraction']<=baseline['mean_absolute_error_fraction']),
            'human_feedback_review_required':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scores',required=True);parser.add_argument('--requests',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args()
    Path(args.output).write_text(json.dumps(compare(args.scores,args.requests),indent=2),encoding='utf-8')


if __name__=='__main__':main()
