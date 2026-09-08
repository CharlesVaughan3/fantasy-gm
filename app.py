from collections import defaultdict
from difflib import SequenceMatcher
from html import unescape
from html.parser import HTMLParser
from itertools import combinations
import math
import re
import subprocess
import time
import unicodedata

import pandas as pd
import requests
import streamlit as st

SLEEPER = "https://api.sleeper.app/v1"
MARKET_URL = "https://fantasyfootballtradeanalyzer.net/trade-value-chart/"
POSITIONS = ["QB", "RB", "WR", "TE"]
PRIORITIES = ["Avoid", "Low", "Neutral", "High", "Priority"]
SECOND_ASSET_WEIGHT = 0.72

st.set_page_config(page_title="Fantasy GM", page_icon="🏈", layout="wide")


def n(x, default=0):
    try:
        return default if x is None else float(x)
    except (TypeError, ValueError):
        return default


def clamp(x, lo=0, hi=100):
    return max(lo, min(hi, x))


def norm_name(name):
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", str(name))
    text = text.encode("ascii", "ignore").decode().lower()
    parts = re.findall(r"[a-z0-9]+", text)
    while parts and parts[-1] in {"jr", "sr", "ii", "iii", "iv", "v"}:
        parts.pop()
    return "".join(parts)


def quality(x):
    return "Excellent" if x >= 95 else "Good" if x >= 90 else "Fair" if x >= 80 else "Low"


def strength(x):
    return "Elite" if x >= 85 else "Strong" if x >= 70 else "Average" if x >= 45 else "Weak" if x >= 25 else "Major Need"


def grade(x):
    return "A+" if x >= 92 else "A" if x >= 87 else "A-" if x >= 82 else "B+" if x >= 77 else "B" if x >= 72 else "B-" if x >= 67 else "C" if x >= 60 else "D"


@st.cache_data(ttl=300)
def sget(path):
    r = requests.get(f"{SLEEPER}{path}", timeout=30, headers={"User-Agent": "FantasyGM/3.5"})
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=86400)
def all_players():
    return sget("/players/nfl")


def get_user(username):
    return sget(f"/user/{username.strip()}")


def get_leagues(uid, season):
    return sget(f"/user/{uid}/leagues/nfl/{season}")


def get_rosters(lid):
    return sget(f"/league/{lid}/rosters")


def get_users(lid):
    return sget(f"/league/{lid}/users")


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.table, self.row, self.cell = [], None, None, None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "table":
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"td", "th"} and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.row:
                self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            if self.table:
                self.tables.append(self.table)
            self.table = None


@st.cache_data(ttl=21600)
def market_html():
    p = subprocess.run(
        ["curl.exe", "--fail", "--silent", "--show-error", "--location", "--compressed",
         "--connect-timeout", "15", "--max-time", "45", "-A", "Mozilla/5.0", MARKET_URL],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=50
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip())
    if not p.stdout.strip():
        raise RuntimeError("Market-value page returned an empty response.")
    return p.stdout.strip().lstrip("\ufeff")


def parse_market(html, dynasty):
    parser = TableParser()
    parser.feed(html)
    value_col = "Dynasty Value" if dynasty else "Redraft Value"
    rows = []

    for table in parser.tables:
        header = None
        start = None
        for i, row in enumerate(table):
            clean = [str(x).strip() for x in row]
            if "Player" in clean and ("Position" in clean or "Pos" in clean) and value_col in clean:
                header, start = clean, i
                break
        if header is None:
            continue

        def ci(*names):
            for name in names:
                if name in header:
                    return header.index(name)
            return None

        ri, ni, ti, pi, vi, tri, tieri = (
            ci("Rank"), ci("Player"), ci("Team"), ci("Position", "Pos"),
            ci(value_col), ci("30-Day", "Trend"), ci("Tier")
        )

        for row in table[start + 1:]:
            if pi is None or vi is None or ni is None or len(row) <= max(pi, vi, ni):
                continue
            pos = row[pi].strip().upper()
            if pos not in POSITIONS:
                continue
            vm = re.search(r"-?\d+(?:\.\d+)?", row[vi].replace(",", ""))
            if not vm:
                continue
            rank = None
            if ri is not None and ri < len(row):
                m = re.search(r"\d+", row[ri])
                rank = int(m.group()) if m else None
            trend = 0
            if tri is not None and tri < len(row):
                txt = row[tri].replace("−", "-").lower()
                m = re.search(r"-?\d+", txt)
                if m:
                    trend = int(m.group())
                    if "down" in txt and trend > 0:
                        trend *= -1
            rows.append({
                "name": row[ni].strip(),
                "norm": norm_name(row[ni]),
                "team": row[ti].strip().upper() if ti is not None and ti < len(row) else "",
                "pos": pos,
                "value": max(1, int(float(vm.group()))),
                "rank": rank,
                "trend": trend,
                "tier": row[tieri].strip() if tieri is not None and tieri < len(row) else "",
            })
        if rows:
            break

    if not rows:
        raise RuntimeError("Could not locate the player-value table.")

    buckets = defaultdict(list)
    for row in rows:
        buckets[row["pos"]].append(row)
    for vals in buckets.values():
        vals.sort(key=lambda x: (-x["value"], x["rank"] or 99999))
        for i, row in enumerate(vals, 1):
            row["pos_rank"] = i
    return rows


def player_candidates(p):
    names = [p.get("full_name"), p.get("search_full_name")]
    names.append(f"{p.get('first_name','')} {p.get('last_name','')}".strip())
    out, seen = [], set()
    for name in names:
        nn = norm_name(name)
        if nn and nn not in seen:
            seen.add(nn)
            out.append(nn)
    return out


def build_value_map(rows, players):
    exact = {(r["norm"], r["pos"]): r for r in rows}
    by_pos = defaultdict(list)
    for r in rows:
        by_pos[r["pos"]].append(r)

    vm = {}
    for pid, p in players.items():
        pos = (p.get("position") or "").upper()
        if pos not in POSITIONS:
            continue
        match = None
        method = None
        cands = player_candidates(p)

        for nn in cands:
            match = exact.get((nn, pos))
            if match:
                method = "Exact"
                break

        if match is None and cands:
            best, second = None, None
            st = (p.get("team") or "").upper()
            for row in by_pos[pos]:
                for nn in cands:
                    score = SequenceMatcher(None, nn, row["norm"]).ratio()
                    if st and row["team"] and st == row["team"]:
                        score += 0.015
                    cand = (score, row)
                    if best is None or score > best[0]:
                        second, best = best, cand
                    elif second is None or score > second[0]:
                        second = cand
            if best:
                second_score = second[0] if second else 0
                if best[0] >= 0.93 and best[0] - second_score >= 0.025:
                    match, method = best[1], "Fuzzy"

        if match:
            vm[str(pid)] = {
                "value": match["value"], "rank": match["rank"], "pos_rank": match["pos_rank"],
                "trend30": match["trend"], "tier": match["tier"], "match_method": method
            }
    return vm


@st.cache_data(ttl=86400, show_spinner=False)
def rostered_players(rostered_ids):
    """Return only players actually rostered in this league.

    This avoids carrying Sleeper's entire NFL player database through every
    Streamlit rerun.
    """
    db = all_players()
    return {pid: db.get(pid, {}) for pid in rostered_ids}


@st.cache_data(ttl=21600, show_spinner=False)
def cached_market_data(dynasty):
    """Download/parse the market chart once every six hours."""
    html = market_html()
    rows = parse_market(html, dynasty)
    plain = " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())
    m = re.search(
        r"Snapshot generated:\s*([A-Za-z]{3}\s+\d{1,2},\s+\d{4},\s+\d{1,2}:\d{2}\s+(?:AM|PM)\s+UTC)",
        plain, flags=re.I
    )
    return rows, m.group(1) if m else "current cached snapshot"


@st.cache_data(ttl=21600, show_spinner=False)
def cached_league_value_map(dynasty, rostered_ids):
    """Match market values only to players rostered in the selected league.

    The old app fuzzy-matched every player in Sleeper's full NFL database on
    every rerun.  This cached league-only map is the main performance fix.
    """
    players = rostered_players(rostered_ids)
    rows, as_of = cached_market_data(dynasty)
    vm = build_value_map(rows, players)
    return vm, len(rows), as_of


def pname(pid, players):
    p = players.get(str(pid), {})
    return p.get("full_name") or f"{p.get('first_name','')} {p.get('last_name','')}".strip() or str(pid)


def ppos(pid, players):
    return players.get(str(pid), {}).get("position", "?") or "?"


def pteam(pid, players):
    return players.get(str(pid), {}).get("team", "FA") or "FA"


def tname(user):
    m = user.get("metadata") or {}
    return m.get("team_name") or user.get("display_name") or user.get("username") or "Unknown Team"


def rstatus(pid, roster):
    pid = str(pid)
    if pid in {str(x) for x in roster.get("starters") or []}:
        return "Starter"
    if pid in {str(x) for x in roster.get("reserve") or []}:
        return "IR"
    if pid in {str(x) for x in roster.get("taxi") or []}:
        return "Taxi"
    return "Bench"


def detect_qb(league):
    slots = league.get("roster_positions") or []
    return 2 if slots.count("QB") >= 2 or any(x in {"SUPER_FLEX", "SUPERFLEX"} for x in slots) else 1


def detect_ppr(league):
    rec = n((league.get("scoring_settings") or {}).get("rec"))
    return min([0.0, 0.5, 1.0], key=lambda x: abs(x - rec))


def detect_dynasty(league):
    s = league.get("settings") or {}
    return s.get("type") == 2 or n(s.get("taxi_slots")) > 0 or n(s.get("max_keepers")) > 8


def market(pid, value_map, players):
    pid = str(pid)
    if pid in value_map:
        return value_map[pid]
    if ppos(pid, players) in POSITIONS:
        return {"value": 1, "rank": None, "pos_rank": None, "trend30": 0,
                "tier": "Unmatched floor", "match_method": "Fallback floor"}
    return {"value": 0, "rank": None, "pos_rank": None, "trend30": 0,
            "tier": "", "match_method": "Not valued"}


def roster_df(roster, players, value_map):
    rows = []
    for pid in roster.get("players") or []:
        m = market(pid, value_map, players)
        rows.append({
            "Player": pname(pid, players), "Pos": ppos(pid, players), "NFL Team": pteam(pid, players),
            "Roster Status": rstatus(pid, roster), "Market Value": int(n(m["value"])),
            "Overall Rank": m["rank"], "Pos Rank": m["pos_rank"], "30-Day Trend": int(n(m["trend30"])),
            "Value Match": m["match_method"], "Tier": m["tier"], "Sleeper ID": str(pid)
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    so = {"Starter": 0, "Bench": 1, "IR": 2, "Taxi": 3}
    po = {"QB": 0, "RB": 1, "WR": 2, "TE": 3, "K": 4, "DEF": 5}
    df["_s"] = df["Roster Status"].map(so).fillna(9)
    df["_p"] = df["Pos"].map(po).fillna(9)
    return df.sort_values(["_s", "_p", "Market Value", "Player"], ascending=[True, True, False, True]).drop(columns=["_s", "_p"]).reset_index(drop=True)


def roster_assets(roster, players, value_map, include_unmatched=True):
    out = []
    for pid in roster.get("players") or []:
        pid = str(pid)
        pos = ppos(pid, players)
        if pos not in POSITIONS:
            continue
        m = market(pid, value_map, players)
        if not include_unmatched and m["match_method"] == "Fallback floor":
            continue
        out.append({
            "pid": pid, "name": pname(pid, players), "pos": pos, "value": int(n(m["value"])),
            "status": rstatus(pid, roster), "match_method": m["match_method"]
        })
    return sorted(out, key=lambda x: x["value"], reverse=True)


def roster_metrics(roster, players, value_map):
    starters = {str(x) for x in roster.get("starters") or []}
    total = starter = bench = matched = count = 0
    by_pos = defaultdict(int)
    unmatched = []
    for a in roster_assets(roster, players, value_map):
        count += 1
        total += a["value"]
        by_pos[a["pos"]] += a["value"]
        if a["match_method"] in {"Exact", "Fuzzy"}:
            matched += 1
        elif a["pid"] in starters:
            unmatched.append(a["name"])
        if a["pid"] in starters:
            starter += a["value"]
        else:
            bench += a["value"]
    return {
        "total": total, "starter": starter, "bench": bench, "weighted": starter + bench * 0.35,
        "by_pos": dict(by_pos), "coverage": matched / count * 100 if count else 100,
        "unmatched_starters": unmatched
    }


def relscore(vals, val):
    vals = [n(x) for x in vals]
    if len(vals) <= 1 or math.isclose(min(vals), max(vals)):
        return 50
    return 100 * (n(val) - min(vals)) / (max(vals) - min(vals))


def analyze(rosters, users, players, value_map):
    a = {}
    for r in rosters:
        rid = int(r["roster_id"])
        u = users.get(str(r.get("owner_id")), {})
        a[rid] = {"team": tname(u), "manager": u.get("display_name") or u.get("username") or str(r.get("owner_id")),
                  **roster_metrics(r, players, value_map)}

    for pos in POSITIONS:
        vals = [t["by_pos"].get(pos, 0) for t in a.values()]
        for t in a.values():
            t.setdefault("scores", {})[pos] = round(relscore(vals, t["by_pos"].get(pos, 0)), 1)

    wvals, tvals = [x["weighted"] for x in a.values()], [x["total"] for x in a.values()]
    for t in a.values():
        t["power"] = round(0.7 * relscore(wvals, t["weighted"]) + 0.3 * relscore(tvals, t["total"]), 1)

    for rank, (rid, _) in enumerate(sorted(a.items(), key=lambda kv: (kv[1]["power"], kv[1]["weighted"]), reverse=True), 1):
        a[rid]["rank"] = rank
    return a


def strong(team):
    return max(POSITIONS, key=lambda p: team["scores"].get(p, 50))


def weak(team):
    return min(POSITIONS, key=lambda p: team["scores"].get(p, 50))


def lineup_slots(roster_positions):
    keep = {"QB", "RB", "WR", "TE", "FLEX", "SUPER_FLEX", "SUPERFLEX", "REC_FLEX", "WRRB_FLEX", "WRRBTE_FLEX", "OP"}
    return [str(x).upper() for x in roster_positions or [] if str(x).upper() in keep]


def best_lineup(assets, roster_positions):
    rem = [dict(a) for a in assets if a["pos"] in POSITIONS]
    chosen = []
    slots = lineup_slots(roster_positions)

    for slot in [s for s in slots if s in POSITIONS]:
        eligible = [a for a in rem if a["pos"] == slot]
        if eligible:
            b = max(eligible, key=lambda x: x["value"])
            chosen.append(b)
            rem.remove(b)

    for slot in [s for s in slots if s not in POSITIONS]:
        allowed = {"RB", "WR", "TE"} if slot in {"FLEX", "REC_FLEX", "WRRB_FLEX", "WRRBTE_FLEX"} else set(POSITIONS)
        eligible = [a for a in rem if a["pos"] in allowed]
        if eligible:
            b = max(eligible, key=lambda x: x["value"])
            chosen.append(b)
            rem.remove(b)

    return {"starter_value": sum(a["value"] for a in chosen), "bench_value": sum(a["value"] for a in rem)}


def starter_delta(before, after):
    return 100 * (after["starter_value"] - before["starter_value"]) / max(before["starter_value"], 1)


def depth_delta(before, after):
    return 100 * (after["bench_value"] - before["bench_value"]) / max(before["bench_value"], 1)


def apply_trade(assets, outgoing, incoming):
    out_ids = {a["pid"] for a in outgoing}
    new = [dict(a) for a in assets if a["pid"] not in out_ids]
    ids = {a["pid"] for a in new}
    for a in incoming:
        if a["pid"] not in ids:
            new.append(dict(a))
    return new


def raw_value(pkg):
    return sum(a["value"] for a in pkg)


def adjusted_value(pkg):
    vals = sorted((a["value"] for a in pkg), reverse=True)
    if not vals:
        return 0
    if len(vals) == 1:
        v = float(vals[0])
        return v * (1.16 if v >= 50 else 1.12 if v >= 40 else 1.08 if v >= 30 else 1)
    return vals[0] + vals[1] * SECOND_ASSET_WEIGHT


def fairness(outgoing, incoming):
    a, b = adjusted_value(outgoing), adjusted_value(incoming)
    return 100 * min(a, b) / max(a, b) if max(a, b) else 0


def pkg_names(pkg):
    return " + ".join(a["name"] for a in pkg)


def tradable(roster, players, value_map, min_value=1, locked=None):
    locked = {str(x) for x in locked or []}
    return [a for a in roster_assets(roster, players, value_map, False)
            if a["value"] >= min_value and a["pid"] not in locked]


def packages(assets, max_size=2, cap=None):
    out = [(a,) for a in assets]
    if max_size >= 2:
        out += list(combinations(assets, 2))
    out.sort(key=lambda p: (adjusted_value(p), raw_value(p)), reverse=True)
    return out[:cap] if cap else out


def pkg_key(pkg):
    return tuple(sorted(a["pid"] for a in pkg))


def team_need(team, pkg):
    total = raw_value(pkg)
    return sum((100 - team["scores"].get(a["pos"], 50)) * a["value"] for a in pkg) / total if total else 50


def sender_surplus(team, pkg):
    total = raw_value(pkg)
    return sum(team["scores"].get(a["pos"], 50) * a["value"] for a in pkg) / total if total else 50


def fit(team, incoming, outgoing):
    return 0.6 * team_need(team, incoming) + 0.4 * sender_surplus(team, outgoing)


def priority_score(pkg, priorities):
    mp = {"Avoid": 0, "Low": 35, "Neutral": 55, "High": 80, "Priority": 100}
    total = raw_value(pkg)
    return sum(mp[priorities.get(a["pos"], "Neutral")] * a["value"] for a in pkg) / total if total else 50


def has_avoid(pkg, priorities):
    return any(priorities.get(a["pos"], "Neutral") == "Avoid" for a in pkg)


def benefit_score(team, before, after, outgoing, incoming):
    sd = starter_delta(before, after)
    dd = depth_delta(before, after)
    return clamp(50 + sd * 5 + dd * 1.5 + (fit(team, incoming, outgoing) - 50) * 0.22 + (fairness(outgoing, incoming) - 85) * 0.35)


def acceptance_score(team, before, after, outgoing, incoming):
    fair = fairness(outgoing, incoming)
    ft = fit(team, incoming, outgoing)
    sd, dd = starter_delta(before, after), depth_delta(before, after)
    value_delta = 100 * (adjusted_value(incoming) - adjusted_value(outgoing)) / max(adjusted_value(outgoing), 1)
    return clamp(0.38 * fair + 0.22 * ft + 0.16 * clamp(50 + value_delta * 2)
                 + 0.16 * clamp(50 + sd * 4) + 0.08 * clamp(50 + dd * 1.5))


def two_way_search(my_rid, rosters, analyses, players, value_map, roster_positions,
                   partner_rids, trade_types, min_fair, min_accept, min_value,
                   locked, priorities, qb_strategy, max_results=25):
    rb = {int(r["roster_id"]): r for r in rosters}
    me = analyses[my_rid]
    my_all = roster_assets(rb[my_rid], players, value_map)
    my_before = best_lineup(my_all, roster_positions)
    my_trade = tradable(rb[my_rid], players, value_map, min_value, locked)
    sizes = {"1 for 1": (1, 1), "2 for 1": (2, 1), "1 for 2": (1, 2), "2 for 2": (2, 2)}
    results = []

    for rid in partner_rids:
        other_all = roster_assets(rb[rid], players, value_map)
        other_before = best_lineup(other_all, roster_positions)
        other_trade = tradable(rb[rid], players, value_map, min_value)

        for typ in trade_types:
            s1, s2 = sizes[typ]
            opkgs = [(a,) for a in my_trade] if s1 == 1 else list(combinations(my_trade, 2))
            ipkgs = [(a,) for a in other_trade] if s2 == 1 else list(combinations(other_trade, 2))
            for outgoing in opkgs:
                for incoming in ipkgs:
                    if qb_strategy == "I'm set at QB" and any(a["pos"] == "QB" for a in incoming):
                        continue
                    if has_avoid(incoming, priorities):
                        continue
                    fair = fairness(outgoing, incoming)
                    if fair < min_fair:
                        continue

                    my_after = best_lineup(apply_trade(my_all, outgoing, incoming), roster_positions)
                    other_after = best_lineup(apply_trade(other_all, incoming, outgoing), roster_positions)
                    my_sd = starter_delta(my_before, my_after)
                    other_sd = starter_delta(other_before, other_after)
                    if my_sd <= 0 or other_sd < -8:
                        continue

                    accept = acceptance_score(analyses[rid], other_before, other_after, incoming, outgoing)
                    if accept < min_accept:
                        continue

                    target = priority_score(incoming, priorities)
                    score = clamp(0.35 * clamp(50 + my_sd * 5) + 0.2 * fair + 0.15 * fit(me, incoming, outgoing)
                                  + 0.15 * accept + 0.1 * target + 0.05 * clamp(50 + depth_delta(my_before, my_after) * 2))
                    results.append({
                        "Grade": grade(score), "Partner": analyses[rid]["team"], "Type": typ,
                        "You Send": pkg_names(outgoing), "You Get": pkg_names(incoming),
                        "Starter Δ%": round(my_sd, 1), "Fairness": round(fair, 1),
                        "Target Fit": round(target, 1), "Acceptance": round(accept, 1),
                        "Trade Score": round(score, 1)
                    })

    results.sort(key=lambda x: (x["Trade Score"], x["Starter Δ%"], x["Acceptance"]), reverse=True)
    seen, out = set(), []
    for row in results:
        key = (row["Partner"], row["You Send"], row["You Get"])
        if key not in seen:
            seen.add(key)
            out.append(row)
        if len(out) >= max_results:
            break
    return out, len(results)


def organization_delta_pct(team_analysis, before, after, outgoing, incoming):
    """Composite whole-roster change.

    Starter value matters most, bench value is discounted, and a small
    position-need bonus rewards trades that solve an actual roster weakness.
    This lets a team accept a small starter downgrade when the overall roster
    genuinely gets better.
    """
    before_weighted = before["starter_value"] + before["bench_value"] * 0.35
    after_weighted = after["starter_value"] + after["bench_value"] * 0.35

    value_delta = (
        100
        *
        (
            after_weighted
            -
            before_weighted
        )
        /
        max(
            before_weighted,
            1,
        )
    )

    need_bonus = (
        fit(
            team_analysis,
            incoming,
            outgoing,
        )
        -
        50
    ) * 0.04

    return value_delta + need_bonus


def smart_packages(
    assets,
    team_analysis,
    max_size=2,
    cap=35,
):
    """Keep all useful single-player options, then fill remaining slots
    with the best two-player packages.

    The old engine simply took the most valuable packages, which could
    completely miss mid-value players that solve another team's real need.
    """
    singles = [
        (
            asset,
        )
        for asset in assets
    ]

    singles.sort(
        key=lambda p: (
            adjusted_value(p),
            sender_surplus(
                team_analysis,
                p,
            ),
        ),
        reverse=True,
    )

    if max_size < 2:
        return singles[:cap]

    pairs = list(
        combinations(
            assets,
            2,
        )
    )

    def pair_score(pkg):
        surplus = sender_surplus(
            team_analysis,
            pkg,
        )

        return (
            adjusted_value(pkg)
            *
            (
                0.80
                +
                surplus
                /
                250
            )
        )

    pairs.sort(
        key=lambda p: (
            pair_score(p),
            adjusted_value(p),
        ),
        reverse=True,
    )

    # Preserve singles first so useful one-for-one legs are never pushed out
    # simply because high-value two-player packages dominate the ranking.
    output = singles[:cap]

    remaining = max(
        0,
        cap
        -
        len(output),
    )

    if remaining:
        output.extend(
            pairs[:remaining]
        )

    return output[:cap]


def three_way_solver(
    my_rid,
    team_b,
    team_c,
    rosters,
    analyses,
    players,
    value_map,
    roster_positions,
    locked,
    priorities,
    qb_strategy,
    min_value=3,
    max_pkg_size=2,
    pkg_cap=35,
    min_fair=72,
    min_org_delta=0.0,
    min_my_sd=0.1,
    max_other_starter_loss=3.0,
    include_flexible_hubs=True,
    max_results=25,
):
    """Search both ordinary 3-team cycles and flexible hub trades.

    Hard requirements:
      * the user's incoming package obeys QB/position preferences;
      * every team meets the market-fairness floor;
      * the user's best starting lineup improves;
      * the other two teams cannot lose more starter strength than allowed;
      * every team's composite organization value meets the requested floor.

    Acceptance and benefit scores are ranking factors rather than separate
    hard gates. This prevents the same trade from being rejected three times
    for closely related metrics.
    """
    rb = {
        int(
            r["roster_id"]
        ):
            r
        for r in rosters
    }

    trio = [
        my_rid,
        team_b,
        team_c,
    ]

    assets = {}
    before = {}
    pkgs = {}

    for rid in trio:
        assets[rid] = roster_assets(
            rb[rid],
            players,
            value_map,
        )

        before[rid] = best_lineup(
            assets[rid],
            roster_positions,
        )

        team_trade_assets = tradable(
            rb[rid],
            players,
            value_map,
            min_value,
            locked
            if rid == my_rid
            else
            None,
        )

        pkgs[rid] = smart_packages(
            team_trade_assets,
            analyses[rid],
            max_pkg_size,
            pkg_cap,
        )

    stats = {
        "package_counts": {
            rid:
                len(
                    pkgs[rid]
                )
            for rid in trio
        },
        "candidate_cycles": 0,
        "strict_cycles_checked": 0,
        "hub_trades_checked": 0,
        "transition_evaluations": 0,
        "rejected_target": 0,
        "rejected_fairness": 0,
        "rejected_my_starter": 0,
        "rejected_other_starter": 0,
        "rejected_org_benefit": 0,
        "qualifying": 0,
    }

    if any(
        not pkgs[rid]
        for rid in trio
    ):
        return [], stats

    transition_cache = {}
    results = []

    def transition(
        rid,
        outgoing,
        incoming,
    ):
        key = (
            rid,
            pkg_key(outgoing),
            pkg_key(incoming),
        )

        if key in transition_cache:
            return transition_cache[key]

        stats[
            "transition_evaluations"
        ] += 1

        fair = fairness(
            outgoing,
            incoming,
        )

        if fair < min_fair:
            result = {
                "fair":
                    fair,
            }

            transition_cache[
                key
            ] = result

            return result

        after = best_lineup(
            apply_trade(
                assets[rid],
                outgoing,
                incoming,
            ),
            roster_positions,
        )

        sd = starter_delta(
            before[rid],
            after,
        )

        dd = depth_delta(
            before[rid],
            after,
        )

        org = organization_delta_pct(
            analyses[rid],
            before[rid],
            after,
            outgoing,
            incoming,
        )

        accept = acceptance_score(
            analyses[rid],
            before[rid],
            after,
            outgoing,
            incoming,
        )

        ben = benefit_score(
            analyses[rid],
            before[rid],
            after,
            outgoing,
            incoming,
        )

        result = {
            "fair":
                fair,

            "sd":
                sd,

            "dd":
                dd,

            "org":
                org,

            "accept":
                accept,

            "benefit":
                ben,
        }

        transition_cache[
            key
        ] = result

        return result

    def consider_candidate(
        topology,
        outgoing_by_team,
        incoming_by_team,
        flow_parts,
    ):
        stats[
            "candidate_cycles"
        ] += 1

        if topology == "Closed Cycle":
            stats[
                "strict_cycles_checked"
            ] += 1

        else:
            stats[
                "hub_trades_checked"
            ] += 1

        my_incoming = incoming_by_team[
            my_rid
        ]

        if (
            qb_strategy
            ==
            "I'm set at QB"
            and
            any(
                a["pos"]
                ==
                "QB"
                for a in my_incoming
            )
        ):
            stats[
                "rejected_target"
            ] += 1

            return

        if has_avoid(
            my_incoming,
            priorities,
        ):
            stats[
                "rejected_target"
            ] += 1

            return

        metrics = {
            rid:
                transition(
                    rid,
                    outgoing_by_team[
                        rid
                    ],
                    incoming_by_team[
                        rid
                    ],
                )
            for rid in trio
        }

        if any(
            m[
                "fair"
            ]
            <
            min_fair
            for m in metrics.values()
        ):
            stats[
                "rejected_fairness"
            ] += 1

            return

        if (
            metrics[
                my_rid
            ][
                "sd"
            ]
            <
            min_my_sd
        ):
            stats[
                "rejected_my_starter"
            ] += 1

            return

        if any(
            metrics[
                rid
            ][
                "sd"
            ]
            <
            -
            max_other_starter_loss
            for rid in trio
            if rid != my_rid
        ):
            stats[
                "rejected_other_starter"
            ] += 1

            return

        if any(
            metrics[
                rid
            ][
                "org"
            ]
            <
            min_org_delta
            for rid in trio
        ):
            stats[
                "rejected_org_benefit"
            ] += 1

            return

        stats[
            "qualifying"
        ] += 1

        target = priority_score(
            my_incoming,
            priorities,
        )

        weak_fair = min(
            m[
                "fair"
            ]
            for m in metrics.values()
        )

        weak_accept = min(
            m[
                "accept"
            ]
            for m in metrics.values()
        )

        weak_org = min(
            m[
                "org"
            ]
            for m in metrics.values()
        )

        avg_org = (
            sum(
                m[
                    "org"
                ]
                for m in metrics.values()
            )
            /
            3
        )

        my_starter_score = clamp(
            50
            +
            metrics[
                my_rid
            ][
                "sd"
            ]
            *
            6
        )

        weak_org_score = clamp(
            50
            +
            weak_org
            *
            8
        )

        avg_org_score = clamp(
            50
            +
            avg_org
            *
            6
        )

        score = clamp(
            0.25
            *
            my_starter_score
            +
            0.20
            *
            weak_org_score
            +
            0.15
            *
            avg_org_score
            +
            0.15
            *
            weak_fair
            +
            0.15
            *
            weak_accept
            +
            0.10
            *
            target
        )

        results.append(
            {
                "Topology":
                    topology,

                "Grade":
                    grade(
                        score
                    ),

                "3-Way Score":
                    round(
                        score,
                        1,
                    ),

                "You Send":
                    pkg_names(
                        outgoing_by_team[
                            my_rid
                        ]
                    ),

                "You Receive":
                    pkg_names(
                        incoming_by_team[
                            my_rid
                        ]
                    ),

                "Your Starter Δ%":
                    round(
                        metrics[
                            my_rid
                        ][
                            "sd"
                        ],
                        1,
                    ),

                "Weakest Team Org Δ%":
                    round(
                        weak_org,
                        1,
                    ),

                "Average Org Δ%":
                    round(
                        avg_org,
                        1,
                    ),

                "Weakest Fairness":
                    round(
                        weak_fair,
                        1,
                    ),

                "Weakest Acceptance":
                    round(
                        weak_accept,
                        1,
                    ),

                "Target Fit":
                    round(
                        target,
                        1,
                    ),

                "Flow":
                    " | ".join(
                        flow_parts
                    ),

                "Outgoing":
                    outgoing_by_team,

                "Incoming":
                    incoming_by_team,

                "Starter Map":
                    {
                        rid:
                            metrics[
                                rid
                            ][
                                "sd"
                            ]
                        for rid in trio
                    },

                "Depth Map":
                    {
                        rid:
                            metrics[
                                rid
                            ][
                                "dd"
                            ]
                        for rid in trio
                    },

                "Org Map":
                    {
                        rid:
                            metrics[
                                rid
                            ][
                                "org"
                            ]
                        for rid in trio
                    },

                "Fair Map":
                    {
                        rid:
                            metrics[
                                rid
                            ][
                                "fair"
                            ]
                        for rid in trio
                    },

                "Accept Map":
                    {
                        rid:
                            metrics[
                                rid
                            ][
                                "accept"
                            ]
                        for rid in trio
                    },

                "Benefit Map":
                    {
                        rid:
                            metrics[
                                rid
                            ][
                                "benefit"
                            ]
                        for rid in trio
                    },
            }
        )

    # ========================================================
    # TOPOLOGY 1: TRADITIONAL CLOSED LOOPS
    # ========================================================

    directions = [
        {
            my_rid:
                team_b,

            team_b:
                team_c,

            team_c:
                my_rid,
        },
        {
            my_rid:
                team_c,

            team_c:
                team_b,

            team_b:
                my_rid,
        },
    ]

    for direction in directions:
        receiver_to_sender = {
            receiver:
                sender
            for sender, receiver
            in direction.items()
        }

        for my_pkg in pkgs[
            my_rid
        ]:
            for b_pkg in pkgs[
                team_b
            ]:
                for c_pkg in pkgs[
                    team_c
                ]:
                    outgoing = {
                        my_rid:
                            my_pkg,

                        team_b:
                            b_pkg,

                        team_c:
                            c_pkg,
                    }

                    incoming = {
                        rid:
                            outgoing[
                                receiver_to_sender[
                                    rid
                                ]
                            ]
                        for rid in trio
                    }

                    flow = [
                        (
                            f"{analyses[sender]['team']} → "
                            f"{analyses[receiver]['team']}: "
                            f"{pkg_names(outgoing[sender])}"
                        )
                        for sender, receiver
                        in direction.items()
                    ]

                    consider_candidate(
                        "Closed Cycle",
                        outgoing,
                        incoming,
                        flow,
                    )

    # ========================================================
    # TOPOLOGY 2: FLEXIBLE HUB TRADE
    #
    # One team sends two different players, one to each of the
    # other teams.  Each of those teams sends one player back to
    # the hub.  This covers many real 3-team constructions that
    # cannot be represented as a perfect circular trade.
    # ========================================================

    if (
        include_flexible_hubs
        and
        max_pkg_size >= 2
    ):
        for hub in trio:
            others = [
                rid
                for rid in trio
                if rid != hub
            ]

            o1, o2 = (
                others[
                    0
                ],
                others[
                    1
                ],
            )

            hub_pairs = [
                pkg
                for pkg in pkgs[
                    hub
                ]
                if len(
                    pkg
                )
                ==
                2
            ]

            o1_singles = [
                pkg
                for pkg in pkgs[
                    o1
                ]
                if len(
                    pkg
                )
                ==
                1
            ]

            o2_singles = [
                pkg
                for pkg in pkgs[
                    o2
                ]
                if len(
                    pkg
                )
                ==
                1
            ]

            for hub_pkg in hub_pairs:
                assignments = [
                    (
                        hub_pkg[
                            0
                        ],
                        hub_pkg[
                            1
                        ],
                    ),
                    (
                        hub_pkg[
                            1
                        ],
                        hub_pkg[
                            0
                        ],
                    ),
                ]

                for o1_pkg in o1_singles:
                    for o2_pkg in o2_singles:
                        for to_o1, to_o2 in assignments:
                            outgoing = {
                                hub:
                                    hub_pkg,

                                o1:
                                    o1_pkg,

                                o2:
                                    o2_pkg,
                            }

                            incoming = {
                                hub:
                                    (
                                        o1_pkg[
                                            0
                                        ],
                                        o2_pkg[
                                            0
                                        ],
                                    ),

                                o1:
                                    (
                                        to_o1,
                                    ),

                                o2:
                                    (
                                        to_o2,
                                    ),
                            }

                            flow = [
                                (
                                    f"{analyses[hub]['team']} → "
                                    f"{analyses[o1]['team']}: "
                                    f"{to_o1['name']}"
                                ),
                                (
                                    f"{analyses[hub]['team']} → "
                                    f"{analyses[o2]['team']}: "
                                    f"{to_o2['name']}"
                                ),
                                (
                                    f"{analyses[o1]['team']} → "
                                    f"{analyses[hub]['team']}: "
                                    f"{o1_pkg[0]['name']}"
                                ),
                                (
                                    f"{analyses[o2]['team']} → "
                                    f"{analyses[hub]['team']}: "
                                    f"{o2_pkg[0]['name']}"
                                ),
                            ]

                            consider_candidate(
                                (
                                    "Flexible Hub — "
                                    f"{analyses[hub]['team']}"
                                ),
                                outgoing,
                                incoming,
                                flow,
                            )

    results.sort(
        key=lambda x: (
            x[
                "3-Way Score"
            ],
            x[
                "Weakest Team Org Δ%"
            ],
            x[
                "Weakest Acceptance"
            ],
            x[
                "Your Starter Δ%"
            ],
        ),
        reverse=True,
    )

    seen = set()
    output = []

    for row in results:
        key = row[
            "Flow"
        ]

        if key in seen:
            continue

        seen.add(
            key
        )

        output.append(
            row
        )

        if len(
            output
        ) >= max_results:
            break

    return output, stats

st.title("🏈 Fantasy GM")
st.caption("Phase 3.5 — flexible 3-way trade solver + rejection diagnostics + fast league-only caching.")

with st.sidebar:
    st.header("Sleeper Connection")
    username = st.text_input("Sleeper username", placeholder="Enter Sleeper username")
    season = st.number_input("Season", min_value=2020, max_value=2035, value=2026, step=1)

if not username:
    st.info("Enter your Sleeper username in the sidebar.")
    st.stop()

try:
    user = get_user(username)
    uid = str(user["user_id"])
    leagues = get_leagues(uid, int(season))
except Exception as e:
    st.error(f"Sleeper connection error: {e}")
    st.stop()

if not leagues:
    st.warning("No Sleeper NFL leagues found.")
    st.stop()

llook = {f"{x.get('name','Unnamed')} — {x.get('total_rosters','?')} teams": x for x in leagues}
league = llook[st.sidebar.selectbox("Choose league", list(llook))]
lid = str(league["league_id"])

load_clock = time.perf_counter()

try:
    rosters, lusers = get_rosters(lid), get_users(lid)
except Exception as e:
    st.error(f"Could not load league data: {e}")
    st.stop()

users = {str(x["user_id"]): x for x in lusers if x.get("user_id")}
my_roster = next((r for r in rosters if str(r.get("owner_id")) == uid), None)
if not my_roster:
    st.error("Could not match your Sleeper account to a roster.")
    st.stop()
my_rid = int(my_roster["roster_id"])

st.sidebar.divider()
st.sidebar.header("Value Settings")
dynasty = st.sidebar.radio("League type", ["Dynasty", "Redraft"], index=0 if detect_dynasty(league) else 1) == "Dynasty"
qb_type = st.sidebar.radio("QB format", ["1 QB", "Superflex / 2 QB"], index=1 if detect_qb(league) == 2 else 0)
ppr_guess = detect_ppr(league)
ppr = st.sidebar.selectbox("League scoring", ["0 PPR", "0.5 PPR", "1 PPR"], index={0.0: 0, 0.5: 1, 1.0: 2}[ppr_guess])

# Build a stable cache key from only the players actually rostered in this league.
rostered_ids = tuple(sorted({
    str(pid)
    for roster in rosters
    for pid in (roster.get("players") or [])
}))

player_clock = time.perf_counter()
players = rostered_players(rostered_ids)
player_load_seconds = time.perf_counter() - player_clock

market_clock = time.perf_counter()
try:
    value_map, market_count, as_of = cached_league_value_map(dynasty, rostered_ids)
    market_error = None
except Exception as e:
    value_map, market_count, as_of, market_error = {}, 0, None, str(e)
market_load_seconds = time.perf_counter() - market_clock

analysis_clock = time.perf_counter()
analyses = analyze(rosters, users, players, value_map)
me = analyses[my_rid]
analysis_seconds = time.perf_counter() - analysis_clock
total_load_seconds = time.perf_counter() - load_clock

st.subheader(league.get("name", "Sleeper League"))
c = st.columns(5)
c[0].metric("Teams", league.get("total_rosters", len(rosters)))
c[1].metric("Format", "Dynasty" if dynasty else "Redraft")
c[2].metric("QB", qb_type)
c[3].metric("Scoring", ppr)
c[4].metric("Your Power Rank", f"#{me['rank']}")

if market_error:
    st.error(f"Market values did not load: {market_error}")
else:
    st.success(f"Market values connected — {market_count} chart assets, {len(value_map)} rostered-player matches. Snapshot: {as_of}.")

with st.sidebar.expander("⚡ Performance", expanded=False):
    st.caption(f"Total page data load: {total_load_seconds:.2f}s")
    st.caption(f"Rostered-player load: {player_load_seconds:.2f}s")
    st.caption(f"Market match/load: {market_load_seconds:.2f}s")
    st.caption(f"League analysis: {analysis_seconds:.3f}s")
    st.caption(f"Rostered players processed: {len(rostered_ids)}")

my_ui_assets = roster_assets(my_roster, players, value_map, False)
player_options = {f"{a['name']} ({a['pos']}, value {a['value']})": a["pid"] for a in my_ui_assets}
bowers_default = [label for label, pid in player_options.items() if norm_name(pname(pid, players)) == "brockbowers"]

tabs = st.tabs(["🏠 My Team", "📊 Power Rankings", "🎯 Position Analysis", "🧠 Smart Trade Finder", "🔺 3-Way Trade Finder", "👥 League Rosters", "🧪 Data Quality"])

with tabs[0]:
    st.header(f"My Team — {me['team']}")
    cc = st.columns(6)
    cc[0].metric("Power Rank", f"#{me['rank']}")
    cc[1].metric("Power Score", f"{me['power']:.1f}/100")
    cc[2].metric("Roster Value", f"{me['total']:,}")
    cc[3].metric("Starter Value", f"{me['starter']:,}")
    cc[4].metric("Direct Match", f"{me['coverage']:.0f}%")
    cc[5].metric("Data Quality", quality(me["coverage"]))

    pc = st.columns(4)
    for i, pos in enumerate(POSITIONS):
        pc[i].metric(pos, f"{me['scores'].get(pos,50):.0f}/100", f"{me['by_pos'].get(pos,0):,} value")
        pc[i].caption(strength(me["scores"].get(pos, 50)))

    mydf = roster_df(my_roster, players, value_map)
    st.dataframe(mydf[["Player", "Pos", "NFL Team", "Roster Status", "Market Value", "Overall Rank", "Pos Rank", "30-Day Trend", "Value Match", "Tier"]], width="stretch", hide_index=True)

with tabs[1]:
    rows = []
    for t in analyses.values():
        rows.append({
            "Rank": t["rank"], "Team": t["team"], "Manager": t["manager"], "Power Score": t["power"],
            "Roster Value": t["total"], "Starter Value": t["starter"],
            "QB": t["scores"].get("QB", 50), "RB": t["scores"].get("RB", 50),
            "WR": t["scores"].get("WR", 50), "TE": t["scores"].get("TE", 50),
            "Biggest Need": weak(t), "Direct Match %": round(t["coverage"], 1)
        })
    st.dataframe(pd.DataFrame(rows).sort_values("Rank"), width="stretch", hide_index=True)

with tabs[2]:
    rows = []
    for t in analyses.values():
        rows.append({
            "Team": t["team"], "Manager": t["manager"],
            "QB": t["scores"].get("QB", 50), "RB": t["scores"].get("RB", 50),
            "WR": t["scores"].get("WR", 50), "TE": t["scores"].get("TE", 50),
            "Strongest": strong(t), "Biggest Need": weak(t)
        })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

with tabs[3]:
    st.header("🧠 Smart Trade Finder")
    locked_labels = st.multiselect("🚫 Do Not Trade", list(player_options), default=bowers_default, key="two_lock")
    locked = [player_options[x] for x in locked_labels]
    qb_strategy = st.selectbox("QB Strategy", ["I'm set at QB", "Open to a QB upgrade"], key="two_qb")

    pc = st.columns(4)
    defaults = {"QB": "Avoid" if qb_strategy == "I'm set at QB" else "Neutral", "RB": "High", "WR": "High", "TE": "Low"}
    priorities = {}
    for i, pos in enumerate(POSITIONS):
        priorities[pos] = pc[i].selectbox(pos, PRIORITIES, index=PRIORITIES.index(defaults[pos]), key=f"twop_{pos}")

    partner_map = {"All Teams": None}
    for rid, t in analyses.items():
        if rid != my_rid:
            partner_map[f"{t['team']} — {t['manager']}"] = rid
    partner_label = st.selectbox("Trade Partner", list(partner_map), key="two_partner")
    types = st.multiselect("Trade Types", ["1 for 1", "2 for 1", "1 for 2", "2 for 2"], default=["1 for 1", "2 for 1", "1 for 2", "2 for 2"])

    s = st.columns(4)
    min_fair = s[0].slider("Minimum Fairness", 70, 100, 85)
    min_accept = s[1].slider("Minimum Acceptance", 40, 100, 62)
    min_value = s[2].number_input("Minimum Player Value", 1, 50, 3)
    max_res = s[3].number_input("Results", 5, 100, 25, 5)

    if st.button("🔎 FIND SMART TRADES", type="primary", width="stretch"):
        selected = partner_map[partner_label]
        rids = [rid for rid in analyses if rid != my_rid] if selected is None else [selected]
        res, total = two_way_search(
            my_rid, rosters, analyses, players, value_map, league.get("roster_positions") or [],
            rids, types, min_fair, min_accept, int(min_value), locked, priorities, qb_strategy, int(max_res)
        )
        st.session_state["two_results"], st.session_state["two_total"] = res, total

    res = st.session_state.get("two_results", [])
    if res:
        st.success(f"Found {st.session_state.get('two_total', len(res)):,} qualifying trades.")
        st.dataframe(pd.DataFrame([{"Rank": i, **r} for i, r in enumerate(res, 1)]), width="stretch", hide_index=True)

with tabs[4]:
    st.header("🔺 3-Way Trade Solver")

    st.success(
        "This version is intentionally less rigid. "
        "It requires the trade to make sense for all three organizations, "
        "but the other managers may accept a small starter downgrade if "
        "their overall roster, depth, value, and position fit improve."
    )

    st.caption(
        "The solver now searches BOTH traditional closed-loop 3-way trades "
        "and flexible hub trades where one team can send a different player "
        "to each of the other two teams."
    )

    team_map = {
        f"{t['team']} — {t['manager']}":
            rid
        for rid, t in analyses.items()
        if rid != my_rid
    }

    labels = list(
        team_map
    )

    c = st.columns(
        2
    )

    team_b_label = c[
        0
    ].selectbox(
        "Team 2",
        labels,
        key=
            "three_b",
    )

    team_b = team_map[
        team_b_label
    ]

    labels_c = [
        x
        for x in labels
        if team_map[
            x
        ]
        !=
        team_b
    ]

    team_c_label = c[
        1
    ].selectbox(
        "Team 3",
        labels_c,
        key=
            "three_c",
    )

    team_c = team_map[
        team_c_label
    ]

    cards = st.columns(
        3
    )

    for box, rid, prefix in [
        (
            cards[
                0
            ],
            my_rid,
            "Your Team",
        ),
        (
            cards[
                1
            ],
            team_b,
            "Team 2",
        ),
        (
            cards[
                2
            ],
            team_c,
            "Team 3",
        ),
    ]:
        with box:
            st.markdown(
                f"**{prefix}: "
                f"{analyses[rid]['team']}**"
            )

            st.caption(
                f"Power #{analyses[rid]['rank']} | "
                f"Strongest: {strong(analyses[rid])} | "
                f"Need: {weak(analyses[rid])}"
            )

    locked_labels = st.multiselect(
        "🚫 My Do Not Trade List",
        list(
            player_options
        ),
        default=
            bowers_default,
        key=
            "three_lock",
    )

    locked = [
        player_options[
            x
        ]
        for x in locked_labels
    ]

    qb_strategy = st.selectbox(
        "My QB Strategy",
        [
            "I'm set at QB",
            "Open to a QB upgrade",
        ],
        key=
            "three_qb",
    )

    st.markdown(
        "#### What I Want Back"
    )

    pc = st.columns(
        4
    )

    defaults = {
        "QB":
            (
                "Avoid"
                if
                qb_strategy
                ==
                "I'm set at QB"
                else
                "Neutral"
            ),

        "RB":
            "High",

        "WR":
            "High",

        "TE":
            "Low",
    }

    priorities = {}

    for i, pos in enumerate(
        POSITIONS
    ):
        priorities[
            pos
        ] = pc[
            i
        ].selectbox(
            pos,
            PRIORITIES,
            index=
                PRIORITIES.index(
                    defaults[
                        pos
                    ]
                ),
            key=
                f"threep_{pos}",
        )

    st.markdown(
        "#### Search Breadth"
    )

    c = st.columns(
        4
    )

    max_pkg = c[
        0
    ].selectbox(
        "Max Assets Sent By Any Team",
        [
            1,
            2,
        ],
        index=1,
        help=(
            "Use 2 to enable package deals and "
            "the flexible hub-trade topology."
        ),
    )

    min_value = c[
        1
    ].number_input(
        "Minimum Player Value",
        1,
        50,
        3,
        key=
            "three_min",
    )

    pkg_cap = c[
        2
    ].slider(
        "Packages Considered Per Team",
        12,
        60,
        35,
        1,
        help=(
            "35 is a much deeper search than the previous 18. "
            "All useful single-player packages are preserved first."
        ),
    )

    max_res = c[
        3
    ].number_input(
        "Results to Show",
        5,
        100,
        25,
        5,
        key=
            "three_res",
    )

    st.markdown(
        "#### Hard Safety Rules"
    )

    c = st.columns(
        4
    )

    min_fair = c[
        0
    ].slider(
        "Minimum Fairness — Every Team",
        60,
        95,
        72,
        1,
        help=(
            "Only this fairness floor is a hard value gate. "
            "Acceptance is now used for ranking instead of rejection."
        ),
    )

    min_org_delta = c[
        1
    ].slider(
        "Minimum Overall Team Benefit %",
        -2.0,
        5.0,
        0.0,
        0.25,
        help=(
            "0 means all three organizations must come out at least "
            "slightly better after starter value, depth, and roster need "
            "are considered together."
        ),
    )

    min_my_sd = c[
        2
    ].slider(
        "My Minimum Starter Improvement %",
        0.0,
        15.0,
        0.1,
        0.1,
    )

    max_other_loss = c[
        3
    ].slider(
        "Other Teams May Lose Starter %",
        0.0,
        10.0,
        3.0,
        0.25,
        help=(
            "A manager may lose a little immediate starter value "
            "if the entire roster improves enough to compensate."
        ),
    )

    include_hubs = st.checkbox(
        "Search flexible hub trades in addition to closed cycles",
        value=True,
        help=(
            "Example: Team B sends one player to you and another player "
            "to Team C, while both of you send a player back to Team B."
        ),
    )

    closed_estimate = (
        2
        *
        int(
            pkg_cap
        )
        **
        3
    )

    st.caption(
        f"Closed-loop ceiling at these settings: "
        f"~{closed_estimate:,} combinations. "
        "Flexible hub candidates are added when enabled. "
        "Lineup simulations are cached and reused."
    )

    signature = (
        lid,
        team_b,
        team_c,
    )

    if (
        st.session_state.get(
            "three_sig"
        )
        !=
        signature
    ):
        st.session_state[
            "three_results"
        ] = []

        st.session_state[
            "three_stats"
        ] = None

        st.session_state[
            "three_ran"
        ] = False

        st.session_state[
            "three_sig"
        ] = signature

    if st.button(
        "🔺 FIND 3-WAY TRADES",
        type="primary",
        width="stretch",
    ):
        search_clock = (
            time.perf_counter()
        )

        with st.spinner(
            f"Solving trades between "
            f"{analyses[my_rid]['team']}, "
            f"{analyses[team_b]['team']}, and "
            f"{analyses[team_c]['team']}..."
        ):
            res, stats = three_way_solver(
                my_rid=
                    my_rid,

                team_b=
                    team_b,

                team_c=
                    team_c,

                rosters=
                    rosters,

                analyses=
                    analyses,

                players=
                    players,

                value_map=
                    value_map,

                roster_positions=
                    (
                        league.get(
                            "roster_positions"
                        )
                        or
                        []
                    ),

                locked=
                    locked,

                priorities=
                    priorities,

                qb_strategy=
                    qb_strategy,

                min_value=
                    int(
                        min_value
                    ),

                max_pkg_size=
                    int(
                        max_pkg
                    ),

                pkg_cap=
                    int(
                        pkg_cap
                    ),

                min_fair=
                    min_fair,

                min_org_delta=
                    min_org_delta,

                min_my_sd=
                    min_my_sd,

                max_other_starter_loss=
                    max_other_loss,

                include_flexible_hubs=
                    include_hubs,

                max_results=
                    int(
                        max_res
                    ),
            )

        stats[
            "search_seconds"
        ] = (
            time.perf_counter()
            -
            search_clock
        )

        st.session_state[
            "three_results"
        ] = res

        st.session_state[
            "three_stats"
        ] = stats

        st.session_state[
            "three_ran"
        ] = True

    res = st.session_state.get(
        "three_results",
        [],
    )

    stats = st.session_state.get(
        "three_stats"
    )

    if stats:
        c = st.columns(
            6
        )

        c[
            0
        ].metric(
            "Your Packages",
            stats[
                "package_counts"
            ].get(
                my_rid,
                0,
            ),
        )

        c[
            1
        ].metric(
            f"{analyses[team_b]['team']} Packages",
            stats[
                "package_counts"
            ].get(
                team_b,
                0,
            ),
        )

        c[
            2
        ].metric(
            f"{analyses[team_c]['team']} Packages",
            stats[
                "package_counts"
            ].get(
                team_c,
                0,
            ),
        )

        c[
            3
        ].metric(
            "Trades Checked",
            f"{stats['candidate_cycles']:,}",
        )

        c[
            4
        ].metric(
            "Qualified",
            f"{stats['qualifying']:,}",
        )

        c[
            5
        ].metric(
            "Search Time",
            f"{stats.get('search_seconds', 0):.2f}s",
        )

        st.markdown(
            "#### Rejection Diagnostics"
        )

        diagnostic_rows = [
            {
                "Result":
                    "Qualified",

                "Count":
                    stats[
                        "qualifying"
                    ],
            },
            {
                "Result":
                    "Rejected — QB / target-position rule",

                "Count":
                    stats[
                        "rejected_target"
                    ],
            },
            {
                "Result":
                    "Rejected — market fairness",

                "Count":
                    stats[
                        "rejected_fairness"
                    ],
            },
            {
                "Result":
                    "Rejected — your starters did not improve",

                "Count":
                    stats[
                        "rejected_my_starter"
                    ],
            },
            {
                "Result":
                    "Rejected — other team's starter loss too large",

                "Count":
                    stats[
                        "rejected_other_starter"
                    ],
            },
            {
                "Result":
                    "Rejected — overall team benefit",

                "Count":
                    stats[
                        "rejected_org_benefit"
                    ],
            },
        ]

        diagnostic_df = (
            pd.DataFrame(
                diagnostic_rows
            )
        )

        total_checked = max(
            stats[
                "candidate_cycles"
            ],
            1,
        )

        diagnostic_df[
            "% of Checked"
        ] = (
            diagnostic_df[
                "Count"
            ]
            /
            total_checked
            *
            100
        ).round(
            1
        )

        st.dataframe(
            diagnostic_df,
            width="stretch",
            hide_index=True,
            column_config={
                "% of Checked":
                    st.column_config.ProgressColumn(
                        min_value=0,
                        max_value=100,
                        format="%.1f%%",
                    ),
            },
        )

        st.caption(
            f"Topology mix: "
            f"{stats['strict_cycles_checked']:,} closed cycles + "
            f"{stats['hub_trades_checked']:,} flexible hub trades."
        )

    if res:
        st.success(
            f"Found {stats['qualifying']:,} qualifying three-team trades. "
            f"Showing the top {len(res)}."
        )

        summary = pd.DataFrame(
            [
                {
                    "Rank":
                        i,

                    "Grade":
                        r[
                            "Grade"
                        ],

                    "Topology":
                        r[
                            "Topology"
                        ],

                    "You Send":
                        r[
                            "You Send"
                        ],

                    "You Receive":
                        r[
                            "You Receive"
                        ],

                    "Your Starter Δ%":
                        r[
                            "Your Starter Δ%"
                        ],

                    "Worst Team Org Δ%":
                        r[
                            "Weakest Team Org Δ%"
                        ],

                    "Weakest Fairness":
                        r[
                            "Weakest Fairness"
                        ],

                    "Weakest Acceptance":
                        r[
                            "Weakest Acceptance"
                        ],

                    "Target Fit":
                        r[
                            "Target Fit"
                        ],

                    "3-Way Score":
                        r[
                            "3-Way Score"
                        ],
                }

                for i, r in enumerate(
                    res,
                    1,
                )
            ]
        )

        st.dataframe(
            summary,
            width="stretch",
            hide_index=True,
        )

        st.subheader(
            "Top 3-Way Breakdowns"
        )

        for rank, r in enumerate(
            res[
                :10
            ],
            1,
        ):
            with st.expander(
                f"#{rank} — "
                f"{r['Grade']} — "
                f"{r['Topology']} — "
                f"You send {r['You Send']} → "
                f"receive {r['You Receive']}",
                expanded=
                    rank
                    ==
                    1,
            ):
                st.markdown(
                    "### Trade Flow"
                )

                for part in r[
                    "Flow"
                ].split(
                    " | "
                ):
                    st.write(
                        f"• {part}"
                    )

                st.markdown(
                    "### Team-by-Team Result"
                )

                breakdown = []

                for rid in [
                    my_rid,
                    team_b,
                    team_c,
                ]:
                    breakdown.append(
                        {
                            "Team":
                                analyses[
                                    rid
                                ][
                                    "team"
                                ],

                            "Sends":
                                pkg_names(
                                    r[
                                        "Outgoing"
                                    ][
                                        rid
                                    ]
                                ),

                            "Receives":
                                pkg_names(
                                    r[
                                        "Incoming"
                                    ][
                                        rid
                                    ]
                                ),

                            "Starter Δ%":
                                round(
                                    r[
                                        "Starter Map"
                                    ][
                                        rid
                                    ],
                                    1,
                                ),

                            "Depth Δ%":
                                round(
                                    r[
                                        "Depth Map"
                                    ][
                                        rid
                                    ],
                                    1,
                                ),

                            "Overall Team Δ%":
                                round(
                                    r[
                                        "Org Map"
                                    ][
                                        rid
                                    ],
                                    1,
                                ),

                            "Fairness":
                                round(
                                    r[
                                        "Fair Map"
                                    ][
                                        rid
                                    ],
                                    1,
                                ),

                            "Acceptance":
                                round(
                                    r[
                                        "Accept Map"
                                    ][
                                        rid
                                    ],
                                    1,
                                ),
                        }
                    )

                st.dataframe(
                    pd.DataFrame(
                        breakdown
                    ),
                    width="stretch",
                    hide_index=True,
                )

                st.write(
                    f"**Why it ranks:** your starter impact is "
                    f"{r['Your Starter Δ%']:+.1f}%, "
                    f"the worst team still has an overall "
                    f"{r['Weakest Team Org Δ%']:+.1f}% result, "
                    f"weakest fairness is "
                    f"{r['Weakest Fairness']:.1f}%, "
                    f"and weakest estimated acceptance is "
                    f"{r['Weakest Acceptance']:.1f}/100."
                )

    elif st.session_state.get(
        "three_ran"
    ):
        st.warning(
            "No trade qualified for these exact three teams. "
            "Use the rejection table above to see which rule is eliminating "
            "the most candidates instead of guessing."
        )

    else:
        st.info(
            "Choose Team 2 and Team 3, then click "
            "**FIND 3-WAY TRADES**."
        )

with tabs[5]:
    rmap = {f"{analyses[int(r['roster_id'])]['team']} — {analyses[int(r['roster_id'])]['manager']}": r for r in rosters}
    label = st.selectbox("Inspect a roster", list(rmap), key="roster_select")
    r = rmap[label]
    rid = int(r["roster_id"])
    st.caption(f"Power #{analyses[rid]['rank']} | Roster value {analyses[rid]['total']:,} | Biggest need {weak(analyses[rid])}")
    df = roster_df(r, players, value_map)
    st.dataframe(df[["Player", "Pos", "NFL Team", "Roster Status", "Market Value", "Overall Rank", "Pos Rank", "30-Day Trend", "Value Match", "Tier"]], width="stretch", hide_index=True)

with tabs[6]:
    rows = []
    for t in analyses.values():
        rows.append({"Team": t["team"], "Direct Match %": round(t["coverage"], 1), "Quality": quality(t["coverage"]),
                     "Unmatched Starters": ", ".join(t["unmatched_starters"])})
    st.dataframe(pd.DataFrame(rows).sort_values("Direct Match %", ascending=False), width="stretch", hide_index=True)

st.divider()
st.caption("Phase 3.5 — flexible 3-way solver with organization-benefit scoring, rejection diagnostics, and cached performance.")
