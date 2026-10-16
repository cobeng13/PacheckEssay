"""Local/container CLI only. Never routed through the public web application."""
import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .config import Settings
from .store import Store


def safe(value):
    text = '' if value is None else str(value)
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) else text


def export_usage(store, folder, start=None, end=None):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    with store.connect() as db:
        rows = db.execute('SELECT * FROM attempts WHERE started>=? AND started<? ORDER BY id', (start or 0, end or 1e20)).fetchall()
        events = db.execute('SELECT e.*,j.labels,j.model FROM events e JOIN jobs j ON j.id=e.job WHERE e.created>=? AND e.created<? ORDER BY e.id', (start or 0, end or 1e20)).fetchall()
    fields = ['attempt_id','job','task','attempt_number','faculty','department','course','assessment','utc','model','settings','status',
              'duration_seconds','provider_request','provider_response','input_tokens','output_tokens','cached_tokens','cache_write_tokens','reasoning_tokens','cost_usd','cost_status','pricing','error']
    summary = defaultdict(lambda: {'attempts': 0, 'success': 0, 'unknown_costs': 0, 'known_cost_usd': Decimal(0)})
    with (folder / 'requests.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fields); writer.writeheader()
        for row in rows:
            labels = json.loads(row['labels'])
            usage = json.loads(row['usage']) if row['usage'] else {}
            day = datetime.fromtimestamp(row['started'], timezone.utc).isoformat()
            record = dict(attempt_id=row['id'],job=row['job'],task=row['task'],attempt_number=row['number'],**labels,
                utc=day,model=row['model'],settings=row['settings'],status=row['status'],
                duration_seconds=round(row['ended']-row['started'],3) if row['ended'] else None,
                provider_request=row['provider_request'],provider_response=row['provider_response'],
                input_tokens=usage.get('input'),output_tokens=usage.get('output'),cached_tokens=usage.get('cached'),
                cache_write_tokens=usage.get('cache_write'),reasoning_tokens=usage.get('reasoning'),
                cost_usd=row['cost'],cost_status='unknown' if row['cost'] is None else 'calculated',pricing=row['pricing'],error=row['error'])
            writer.writerow({k:safe(v) for k,v in record.items()})
            group = summary[(labels['faculty'],day[:10],row['model'])]
            group['attempts'] += 1; group['success'] += row['status'] == 'success'
            group['unknown_costs'] += row['cost'] is None
            group['known_cost_usd'] += Decimal(row['cost']) if row['cost'] is not None else Decimal(0)
    with (folder / 'summary.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(['faculty','utc_date','model','attempts','successful_grades','unknown_costs','known_cost_usd'])
        for key,value in sorted(summary.items()):
            writer.writerow([safe(v) for v in (*key,value['attempts'],value['success'],value['unknown_costs'],value['known_cost_usd'])])
    with (folder / 'events.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(['job','task','utc','faculty','model','kind'])
        for row in events:
            writer.writerow([safe(v) for v in (row['job'],row['task'],datetime.fromtimestamp(row['created'],timezone.utc).isoformat(),json.loads(row['labels'])['faculty'],row['model'],row['kind'])])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command',required=True)
    usage = sub.add_parser('export-usage'); usage.add_argument('--output',required=True)
    usage.add_argument('--from-date');usage.add_argument('--to-date',help='Exclusive UTC date')
    sub.add_parser('cleanup')
    reconcile=sub.add_parser('reconcile-cost',help='Record a verified provider cost for an uncertain attempt')
    reconcile.add_argument('--attempt',type=int,required=True);reconcile.add_argument('--usd',type=Decimal,required=True)
    args=parser.parse_args();store=Store(Settings())
    if args.command=='export-usage':
        convert=lambda value: datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() if value else None
        export_usage(store,args.output,convert(args.from_date),convert(args.to_date))
        print('Wrote requests.csv, summary.csv and events.csv.')
    elif args.command=='cleanup':
        store.cleanup();print('Expired content removed.')
    else:
        if not args.usd.is_finite() or args.usd<0:
            parser.error('Cost must be finite and nonnegative.')
        with store.connect() as db:
            cursor=db.execute("UPDATE attempts SET cost=?,error='cost_reconciled_by_admin' WHERE id=? AND cost IS NULL AND status!='sending'",(str(args.usd),args.attempt))
            if not cursor.rowcount:
                parser.error('Attempt is missing, active, or already has a known cost.')
        print('Verified cost recorded; original request status retained.')


if __name__=='__main__':
    main()
