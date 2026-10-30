"""The common latest valid build selection; filter only AFTER selecting per person."""


def latest_views(basis):
    latest = {}
    for row in basis['views']:
        old = latest.get(row['player'])
        if old is None or (row['recorded_at'], row.get('received_at', 0), row['digest']) > (old['recorded_at'], old.get('received_at', 0), old['digest']):
            latest[row['player']] = row
    return latest
