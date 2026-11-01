"""Card investment belongs to current people and latest builds, never replay frequency."""
from .player_statistics import query as player_query
from pathlib import Path
from .statistics_local_cards import card_details


def summary(values, population):
    """values=(observed copies, exact partition complete), missing people remain unknown."""
    positive = sum(copies > 0 for copies, complete in values)
    absent = sum(copies == 0 and complete for copies, complete in values)
    known_copies = sum(complete for copies, complete in values)
    bins = {str(n): sum(complete and copies == n for copies, complete in values) for n in (0, 1, 2, 3)}
    bins['4+'] = sum(complete and copies >= 4 for copies, complete in values)
    unknown = population - positive - absent
    return dict(confirmed_players=positive, confirmed_absent_players=absent, unknown_players=unknown,
                population=population, percentage=100 * positive / population if population else None,
                lower_bound=unknown > 0, known_copy_players=known_copies,
                unknown_copy_players=population - known_copies, bins=bins,
                recorded_bins={**{str(n): sum(copies == n for copies, complete in values) for n in (1, 2, 3)},
                               '4+': sum(copies >= 4 for copies, complete in values)},
                recorded_copies=sum(copies for copies, complete in values),
                anomalous_copy_players=sum(complete and copies > 3 for copies, complete in values))


def query(db, replay_root, code, now, deck_types=(), series=(), basis='observed'):
    if basis not in ('observed', 'initial'):
        raise ValueError('invalid_basis')
    seed = player_query(db, replay_root, code, now, deck_types, series)
    metadata = card_details(Path(db.execute('PRAGMA database_list').fetchone()[2]).parent, code)
    base = metadata['base_partition'] or 'main'
    codes = seed['card_identity']['codes']
    placeholders = ','.join('?' for _ in codes)
    copies = {(r['build'], r['partition']): r['copies'] for r in db.execute(
        f'SELECT build,partition,SUM(copies) copies FROM stat_build_cards WHERE code IN ({placeholders}) GROUP BY build,partition', codes)}
    all_values = {p: [] for p in ('main', 'side', 'total', 'extra')}
    groups = {}
    selected_sources = 0
    for person in seed['people']:
        group = groups.setdefault(person['deck_type'], dict(deck_type=person['deck_type'], players=0, values={p: [] for p in all_values}))
        group['players'] += 1
        digest = person['digest'] if basis == 'observed' else person['initial_source_digest']
        if digest is None:
            continue
        row = db.execute('SELECT v.build,b.main_complete,b.side_complete,b.extra_complete FROM stat_views v JOIN stat_builds b ON b.fingerprint=v.build WHERE v.digest=? AND v.player=?', (digest, person['player'])).fetchone()
        selected_sources += 1
        values = {p: (copies.get((row['build'], p), 0), bool(row[p + '_complete'])) for p in ('main', 'side', 'extra')}
        values['total'] = (values[base][0] + values['side'][0], values[base][1] and values['side'][1])
        for part in all_values:
            all_values[part].append(values[part]); group['values'][part].append(values[part])
    population = seed['filtered_players']
    return dict(generated_at=now, card=seed['card'], card_identity=seed['card_identity'], roster=seed['roster'],
                card_details=metadata, basis=basis, current_players=seed['current_players'], covered_players=seed['covered_players'],
                missing_players=seed['missing_players'], population=population, selected_sources=selected_sources,
                unknown_classification_players=sum(g['players'] for g in seed['deck_types'] if g['deck_type'] is None),
                filters=dict(deck_types=list(deck_types), series=list(series)),
                partitions={p: summary(v, population) for p, v in all_values.items()},
                by_deck=[dict(deck_type=g['deck_type'], players=g['players'], partitions={p: summary(v, g['players']) for p, v in g['values'].items()}) for g in sorted(groups.values(), key=lambda g: (-g['players'], g['deck_type'] or ''))],
                semantics='current_top100_one_latest_build_per_person_not_small_games',
                game_filters_do_not_apply=['opponent', 'opponent_series', 'first', 'internal'],
                data_quality=seed['data_quality'])
