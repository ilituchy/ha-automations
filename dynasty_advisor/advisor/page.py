"""HTML for the Home Assistant page, rendered from a run's structured view.

Design rules (from the dataviz skill's reference palette and mark specs):

* Surfaces and text use the palette's light/dark steps; dark mode is its own
  selected set under ``prefers-color-scheme`` (the ingress iframe inherits it;
  HA's theme variables do not cross the iframe).
* Identity colours (positions, team modes) sit on a small dot or bar beside
  the text; **text always wears text tokens**, never the series colour.
* Gains and losses carry a ▲/▼ glyph plus the word, so direction is never
  colour alone.
* Magnitudes that are compared down a column (lineup strength, softness) get
  a thin meter: blue fill on a lighter blue track; numbers in columns use
  tabular figures, standalone numbers proportional ones.

Everything interpolated is escaped: names come from Sleeper and FantasyCalc.
"""

from __future__ import annotations

import html
from typing import Dict, List, Optional

POS_ORDER = ("QB", "RB", "WR", "TE", "PICK")


def _e(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def _n(x) -> str:
    return "{:,.0f}".format(x)


def _signed(x: float, unit: str = "") -> str:
    if x > 0:
        return "<span class='delta up'><i aria-hidden=true>▲</i>+%s%s</span>" % (_n(x), unit)
    if x < 0:
        return "<span class='delta down'><i aria-hidden=true>▼</i>−%s%s</span>" % (_n(-x), unit)
    return "<span class=delta>0</span>"


CSS = """
:root { color-scheme: light dark;
  --bg:#f3f3f1; --surface:#fcfcfb; --raised:#ffffff; --sunk:#f0f0ed;
  --text:#0b0b0b; --text-2:#52514e; --text-3:#7c7b77; --line:#e6e5e1; --line-2:#d9d8d3;
  --accent:#2a78d6; --accent-ink:#ffffff; --track:#cde2fb; --fill:#2a78d6;
  --good:#0ca30c; --bad:#d03b3b; --warn-bg:#fdf3dc; --warn-line:#f0d58f; --warn-ink:#5c4300;
  --qb:#2a78d6; --rb:#1baf7a; --wr:#eb6834; --te:#6250d6; --pick:#8d8c86;
  --seller:#e34948; --bubble:#eda100; --contender:#2a78d6; --middle:#a3a29c;
  --shadow: 0 1px 2px rgba(20,20,15,.04), 0 2px 8px rgba(20,20,15,.04); }
@media (prefers-color-scheme: dark) { :root {
  --bg:#111110; --surface:#1a1a19; --raised:#212120; --sunk:#161615;
  --text:#f0efec; --text-2:#c3c2b7; --text-3:#8f8e86; --line:#2b2b29; --line-2:#363633;
  --accent:#3987e5; --accent-ink:#0b0b0b; --track:#184f95; --fill:#5598e7;
  --good:#3fbf3f; --bad:#e66767; --warn-bg:#2e2611; --warn-line:#5a4a1c; --warn-ink:#f3d68a;
  --qb:#3987e5; --rb:#199e70; --wr:#d95926; --te:#9085e9; --pick:#77766f;
  --seller:#e66767; --bubble:#c98500; --contender:#3987e5; --middle:#6b6a64;
  --shadow: none; } }
* { box-sizing:border-box; }
html { -webkit-text-size-adjust:100%; }
body { margin:0; background:var(--bg); color:var(--text);
  font:15px/1.5 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, sans-serif; }
main { max-width:980px; margin:0 auto; padding:20px 16px 48px; }
h1,h2,h3 { margin:0; font-weight:650; letter-spacing:-.01em; }
.muted { color:var(--text-2); } .faint { color:var(--text-3); }
.small { font-size:13px; }

/* header */
.head { display:flex; flex-wrap:wrap; gap:14px; align-items:center; justify-content:space-between; margin-bottom:14px; }
.brand h1 { font-size:22px; } .brand .sub { font-size:13px; color:var(--text-2); margin-top:2px; }
form.run { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
select { font:inherit; font-size:14px; padding:8px 30px 8px 12px; border-radius:10px; border:1px solid var(--line-2);
  background:var(--raised); color:var(--text); appearance:none;
  background-image: linear-gradient(45deg, transparent 50%, var(--text-2) 50%), linear-gradient(135deg, var(--text-2) 50%, transparent 50%);
  background-position: calc(100% - 15px) 52%, calc(100% - 10px) 52%; background-size:5px 5px; background-repeat:no-repeat; }
button { font:inherit; font-size:14px; font-weight:600; padding:8px 16px; border-radius:10px; border:none;
  background:var(--accent); color:var(--accent-ink); cursor:pointer; }
button:hover { filter:brightness(1.06); } button[disabled] { opacity:.6; cursor:default; filter:none; }
.check { font-size:13px; color:var(--text-2); display:flex; align-items:center; gap:6px; }
.status { font-size:13px; color:var(--text-2); margin:-4px 0 14px; display:flex; flex-wrap:wrap; gap:4px 14px; }
.spin { display:inline-block; width:12px; height:12px; margin-right:7px; vertical-align:-2px; border-radius:50%;
  border:2px solid currentColor; border-right-color:transparent; animation:spin .7s linear infinite; }
@keyframes spin { to { transform:rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { display:none; } }

/* banners */
.banner { display:flex; gap:10px; align-items:flex-start; padding:12px 14px; border-radius:12px; margin-bottom:14px;
  background:var(--warn-bg); border:1px solid var(--warn-line); color:var(--warn-ink); font-size:14px; }
.banner b { font-weight:650; }
.banner.err { background:color-mix(in srgb, var(--bad) 12%, var(--surface)); border-color:color-mix(in srgb, var(--bad) 35%, transparent); color:var(--text); }

/* stat tiles */
.tiles { display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:10px; margin-bottom:26px; }
.tile { background:var(--surface); border:1px solid var(--line); border-radius:14px; padding:12px 14px; box-shadow:var(--shadow); }
.tile .label { font-size:12px; color:var(--text-2); }
.tile .value { font-size:22px; font-weight:650; margin-top:2px; line-height:1.2; }
.tile .note { font-size:12px; color:var(--text-3); margin-top:2px; }
@media (max-width:640px) { .tiles { grid-template-columns:repeat(2, minmax(0,1fr)); } }

/* sections */
section { margin-bottom:30px; }
.sec-head { display:flex; align-items:baseline; justify-content:space-between; gap:10px; margin-bottom:10px; }
.sec-head h2 { font-size:17px; }
.card { background:var(--surface); border:1px solid var(--line); border-radius:16px; box-shadow:var(--shadow); }

/* chips */
.chip { display:inline-flex; align-items:center; gap:6px; font-size:12px; font-weight:550; color:var(--text);
  padding:2px 9px 2px 8px; border-radius:999px; background:var(--sunk); border:1px solid var(--line); white-space:nowrap; }
.dot { width:8px; height:8px; border-radius:50%; flex:none; background:var(--middle); }
.dot.seller { background:var(--seller); } .dot.bubble { background:var(--bubble); }
.dot.contender { background:var(--contender); } .dot.middle { background:var(--middle); }
.pos { display:inline-flex; align-items:center; justify-content:center; min-width:34px; height:20px; padding:0 6px;
  border-radius:6px; font-size:11px; font-weight:700; letter-spacing:.02em; color:var(--text);
  background:var(--sunk); border-left:3px solid var(--pick); }
.pos.QB { border-left-color:var(--qb); } .pos.RB { border-left-color:var(--rb); }
.pos.WR { border-left-color:var(--wr); } .pos.TE { border-left-color:var(--te); }

/* trade cards */
.trades { display:grid; gap:12px; }
.trade { padding:16px 18px; }
.t-top { display:flex; align-items:flex-start; gap:12px; }
.rank { flex:none; width:28px; height:28px; border-radius:50%; background:var(--sunk); border:1px solid var(--line);
  display:flex; align-items:center; justify-content:center; font-size:13px; font-weight:700; color:var(--text-2); }
.t-title { flex:1; min-width:0; }
.t-title h3 { font-size:16px; display:flex; flex-wrap:wrap; align-items:center; gap:8px; }
.t-title .ctx { font-size:13px; color:var(--text-2); margin-top:2px; }
.chance { flex:none; text-align:right; font-size:12px; color:var(--text-2); }
.pips { display:flex; gap:3px; justify-content:flex-end; margin-bottom:3px; }
.pips span { width:14px; height:5px; border-radius:3px; background:var(--track); }
.pips span.on { background:var(--fill); }
.swap { display:grid; grid-template-columns:1fr auto 1fr; gap:12px; align-items:start; margin:14px 0 12px; }
.side { background:var(--sunk); border:1px solid var(--line); border-radius:12px; padding:10px 12px; min-width:0; }
.side .lab { font-size:11px; font-weight:650; text-transform:uppercase; letter-spacing:.06em; color:var(--text-3); margin-bottom:6px; }
.arrow { align-self:center; color:var(--text-3); font-size:18px; }
.asset { display:flex; align-items:center; gap:8px; padding:5px 0; }
.asset + .asset { border-top:1px solid var(--line); }
.asset .nm { flex:1; min-width:0; font-weight:600; font-size:14px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.asset .nm small { font-weight:400; color:var(--text-3); margin-left:4px; }
.asset .val { font-size:13px; color:var(--text-2); font-variant-numeric:tabular-nums; }
@media (max-width:640px) { .swap { grid-template-columns:1fr; } .arrow { transform:rotate(90deg); justify-self:center; margin:-4px 0; } }
.metrics { display:flex; flex-wrap:wrap; gap:8px 22px; font-size:13px; color:var(--text-2); }
.metrics b { color:var(--text); font-weight:600; margin-left:4px; }
.delta { font-weight:650; color:var(--text); font-variant-numeric:tabular-nums; }
.delta i { font-style:normal; font-size:10px; margin-right:3px; vertical-align:1px; }
.delta.up i { color:var(--good); } .delta.down i { color:var(--bad); }
.notes { margin:12px 0 0; padding:0; list-style:none; font-size:13px; color:var(--text-2); }
.notes li { position:relative; padding-left:14px; margin:3px 0; }
.notes li::before { content:""; position:absolute; left:2px; top:.62em; width:4px; height:4px; border-radius:50%; background:var(--text-3); }
details.ceil { margin-top:12px; border-top:1px solid var(--line); padding-top:10px; }
details.ceil summary { cursor:pointer; font-size:13px; font-weight:600; color:var(--text-2); list-style:none; }
details.ceil summary::-webkit-details-marker { display:none; }
details.ceil summary::before { content:"›"; display:inline-block; margin-right:6px; transition:transform .15s; }
details.ceil[open] summary::before { transform:rotate(90deg); }
details.ceil .swap { margin:10px 0 8px; }
.rv { margin-top:12px; padding:10px 12px; border-radius:10px; background:var(--sunk); border-left:3px solid var(--text-3); font-size:13.5px; }
.rv p { margin:4px 0 0; color:var(--text); line-height:1.45; }
.rv p.rv-watch { color:var(--text-2); }
.rv.go { border-left-color:var(--good); } .rv.consider { border-left-color:var(--bubble); } .rv.pass { border-left-color:var(--bad); }
.rv-tag { font-size:11px; font-weight:700; letter-spacing:.06em; text-transform:uppercase; color:var(--text-2); }
.move .rv { margin-top:8px; }
.card.overview { padding:14px 16px; margin:0 0 14px; }
.card.overview p { margin:4px 0 0; font-size:14px; line-height:1.5; }
details.passed { margin-top:14px; padding:12px 16px; }
details.passed summary { cursor:pointer; font-weight:600; color:var(--text-2); }
details.passed .trade { margin-top:12px; box-shadow:none; }

/* moves */
.moves { display:grid; gap:10px; }
.move { display:flex; align-items:center; gap:12px; padding:12px 14px; }
.move .verb { flex:none; font-size:11px; font-weight:700; letter-spacing:.06em; text-transform:uppercase;
  color:var(--text-2); background:var(--sunk); border:1px solid var(--line); border-radius:6px; padding:3px 7px; min-width:44px; text-align:center; }
.move .what { flex:1; min-width:0; } .move .what b { font-weight:600; }
.move .why { font-size:12px; color:var(--text-3); }
.rosterline { font-size:13px; color:var(--text-2); display:flex; flex-wrap:wrap; gap:4px 16px; margin-bottom:10px; }

/* tables */
.tbl { overflow-x:auto; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th { text-align:left; font-weight:600; color:var(--text-3); font-size:12px; padding:10px 12px; border-bottom:1px solid var(--line); white-space:nowrap; }
td { padding:9px 12px; border-bottom:1px solid var(--line); white-space:nowrap; font-variant-numeric:tabular-nums; }
tr:last-child td { border-bottom:none; }
tr.me td { background:color-mix(in srgb, var(--accent) 7%, transparent); }
tr.me td:first-child { box-shadow:inset 3px 0 0 var(--accent); }
td.num, th.num { text-align:right; }
.meter { display:inline-block; width:90px; height:6px; border-radius:3px; background:var(--track); vertical-align:middle; margin-right:8px; overflow:hidden; }
.meter > span { display:block; height:100%; border-radius:3px; background:var(--fill); }
.tag { font-size:11px; color:var(--text-3); margin-left:6px; }

/* picks */
.picks { display:grid; gap:10px; padding:14px 16px; }
.pyear { display:flex; flex-wrap:wrap; align-items:center; gap:8px; }
.pyear .yr { width:46px; font-weight:650; font-size:14px; }
.pk { display:inline-flex; align-items:baseline; gap:6px; padding:5px 10px; border-radius:10px; background:var(--sunk);
  border:1px solid var(--line); font-size:13px; font-weight:600; }
.pk small { font-weight:400; color:var(--text-3); font-variant-numeric:tabular-nums; }
.core { font-size:10px; font-weight:700; letter-spacing:.05em; text-transform:uppercase; color:var(--text-2);
  border:1px solid var(--line-2); border-radius:5px; padding:0 4px; margin-left:6px; vertical-align:1px; }
.prot { display:flex; flex-wrap:wrap; gap:8px; padding:14px 16px; }
.prot .pk { align-items:center; }
.pk.r1 { border-color:color-mix(in srgb, var(--accent) 45%, var(--line)); }

.history { display:flex; flex-wrap:wrap; gap:6px; }
.history a { font-size:12px; text-decoration:none; color:var(--text-2); padding:4px 10px; border-radius:999px; border:1px solid var(--line); background:var(--surface); }
.history a.cur { color:var(--text); border-color:var(--accent); font-weight:600; }
footer { font-size:12px; color:var(--text-3); margin:4px 0 32px; line-height:1.6; max-width:760px; }
.legacy h2 { font-size:17px; margin:22px 0 8px; } .legacy h3 { font-size:15px; margin:14px 0 6px; }
.legacy blockquote { margin:8px 0; padding:10px 12px; border-radius:10px; background:var(--warn-bg); color:var(--warn-ink); }
"""

MODE_LABEL = {"seller": "Selling", "bubble": "Bubble", "contender": "Contending", "middle": "Middle"}
CHANCE_PIPS = {"likely": 3, "plausible": 2}


def _pl(a: Dict, key: str = "name") -> str:
    """A player's name, linked to his card when the view carries his id."""
    name = _e(a.get(key))
    pid = a.get("id")
    if not pid:
        return name
    return "<a class=pl data-card href='player?id=%s'>%s</a>" % (_e(pid), name)


def _mode_chip(mode: str) -> str:
    return "<span class=chip><span class='dot %s'></span>%s</span>" % (_e(mode), _e(MODE_LABEL.get(mode, mode)))


def _asset_row(a: Dict) -> str:
    pos = a.get("pos") or ""
    tag = "PICK" if a.get("kind") == "pick" else pos
    extra = []
    if a.get("age"):
        extra.append(str(a["age"]))
    if a.get("via"):
        extra.append("via " + a["via"])
    core = "<span class=core title='Young and rising: protected'>core</span>" if a.get("core") else ""
    return ("<div class=asset><span class='pos %s'>%s</span><span class=nm>%s%s%s</span><span class=val>%s</span></div>"
            % (_e(tag), _e(tag), _pl(a),
               "<small>%s</small>" % _e(" · ".join(extra)) if extra else "", core, _n(a.get("value") or 0)))


def _swap(pk: Dict) -> str:
    return ("<div class=swap><div class=side><div class=lab>You give</div>%s</div>"
            "<div class=arrow aria-hidden=true>⇄</div>"
            "<div class=side><div class=lab>You get</div>%s</div></div>"
            % ("".join(_asset_row(a) for a in pk["give"]), "".join(_asset_row(a) for a in pk["get"])))


def _metrics(pk: Dict) -> str:
    m = ["<span>Market value%s</span>" % ("<b>%s</b>" % _signed(pk["market"])),
         "<span>Your lineup, rest of season<b>%s</b></span>" % _signed(pk["lineup"])]
    rc = pk.get("roster_change") or 0
    if rc > 0:
        m.append("<span>Roster<b>drop %d</b></span>" % rc)
    elif rc < 0:
        m.append("<span>Roster<b>frees %d spot%s</b></span>" % (-rc, "" if rc == -1 else "s"))
    return "<div class=metrics>%s</div>" % "".join(m)


VERDICT_LABEL = {"go": "Reviewer: go", "consider": "Reviewer: consider", "pass": "Reviewer: pass"}


def _review(r: Dict) -> str:
    if not r:
        return ""
    watch = "<p class=rv-watch>%s</p>" % _e(r["watch"]) if r.get("watch") else ""
    return ("<div class='rv %s'><span class=rv-tag>%s</span><p>%s</p>%s</div>"
            % (_e(r["verdict"]), _e(VERDICT_LABEL.get(r["verdict"], r["verdict"])), _e(r.get("take")), watch))


def _trade(i: int, t: Dict) -> str:
    pips = CHANCE_PIPS.get(t["chance"], 1)
    chance = t["chance"].split(" (")[0]
    hist = ""
    if t.get("accepted") is not None:
        hist = "%s accepted %d · proposed %d since %s" % (t["partner"], t["accepted"], t["proposed"], t.get("since") or "?")
    notes = list(t.get("notes") or [])
    if hist:
        notes.append(hist)
    ceil = ""
    if t.get("ceiling"):
        ceil = ("<details class=ceil><summary>Walk-away ceiling — the most to give and still come out ahead</summary>%s%s</details>"
                % (_swap(t["ceiling"]), _metrics(t["ceiling"])))
    else:
        ceil = "<details class=ceil><summary>Walk-away ceiling</summary><p class='small muted'>Don't add to the opening offer.</p></details>"
    return """<article class='card trade'>
<div class=t-top><div class=rank>%d</div>
<div class=t-title><h3>%s %s</h3><div class=ctx>%s</div></div>
<div class=chance><div class=pips aria-hidden=true>%s</div>%s</div></div>
%s%s%s<ul class=notes>%s</ul>%s</article>""" % (
        i, _e(t["partner"]), _mode_chip(t["mode"]), _e(t.get("context")),
        "".join("<span class=on></span>" if k < pips else "<span></span>" for k in range(3)), _e(chance),
        _swap(t["opening"]), _metrics(t["opening"]), _review(t.get("review")),
        "".join("<li>%s</li>" % _e(n) for n in notes), ceil)


def _tiles(v: Dict) -> str:
    me, dl = v["me"], v.get("deadline")
    tiles = [
        ("Record", me["record"], "projected %s of %d" % (_ord(me["finish"]), me["teams"])),
        ("Position", MODE_LABEL.get(me["mode"], me["mode"]), "strategy: %s" % v["strategy"]),
    ]
    if dl:
        if dl["state"] == "closed":
            tiles.append(("Trade deadline", "Closed", "since week %d" % dl["week"]))
        else:
            left = dl.get("weeks_left", 0)
            tiles.append(("Trade deadline", "Week %d" % dl["week"],
                          "this week" if left <= 0 else "%d week%s away" % (left, "" if left == 1 else "s")))
    if me.get("faab_left") is not None:
        tiles.append(("FAAB left", "$%d" % me["faab_left"], "of $%d" % me["faab_budget"] if me.get("faab_budget") else ""))
    return "<div class=tiles>%s</div>" % "".join(
        "<div class=tile><div class=label>%s</div><div class=value>%s</div><div class=note>%s</div></div>"
        % (_e(a), _e(b), _e(c)) for a, b, c in tiles)


def _ord(n: int) -> str:
    return "%d%s" % (n, "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))


def _moves(m: Dict) -> str:
    rows = []
    if m["ir"] and m["reserve_open"]:
        names = ", ".join("%s (%s)" % (_pl(x), _e(x["status"])) for x in m["ir"])
        rows.append("<div class='card move'><span class=verb>IR</span><div class=what>"
                    "<b>Move %d to IR</b> <span class=muted>— choose from %s</span>"
                    "<div class=why>%d open IR spot%s; each move frees an active roster spot at no cost</div></div></div>"
                    % (min(m["reserve_open"], len(m["ir"])), names, m["reserve_open"], "" if m["reserve_open"] == 1 else "s"))
    for a in m["actions"]:
        add, drop = a["add"], a["drop"]
        why = []
        if add.get("starts"):
            why.append("would start for you")
        if add.get("depth"):
            why.append("depth chart #%s" % add["depth"])
        if add.get("injury"):
            why.append(add["injury"])
        what = "<b>%s</b> <span class=muted>%s %s · %s</span>" % (
            _pl(add), _e(add.get("team") or ""), _e(add.get("pos")), _n(add.get("value") or 0))
        if drop:
            what += " <span class=muted>for</span> <b>%s</b> <span class=muted>%s</span>" % (_pl(drop), _n(drop.get("value") or 0))
        else:
            why.insert(0, "into an open spot")
        rows.append("<div class='card move'><span class=verb>Add</span><div class=what>%s<div class=why>%s</div>%s</div></div>"
                    % (what, _e(" · ".join(why)), _review(a.get("review"))))
    if not rows:
        rows.append("<div class='card move'><div class='what muted'>No pickup clears the bar over your weakest active player.</div></div>")
    line = "<div class=rosterline><span>Active roster %d/%d</span><span>IR spots open %d</span><span>Open after IR moves %d</span></div>" % (
        m["active"], m["active_max"], m["reserve_open"], m["open_spots"])
    fa = ""
    if m["free_agents"]:
        fa = ("<div class='card tbl' style='margin-top:10px'><table><thead><tr><th>Best available</th><th>Pos</th><th class=num>Age</th>"
              "<th class=num>Dynasty</th><th class=num>Rest of season</th><th>Starts for you</th></tr></thead><tbody>%s</tbody></table></div>"
              % "".join("<tr><td><b>%s</b><span class=tag>%s</span></td><td><span class='pos %s'>%s</span></td><td class=num>%s</td>"
                        "<td class=num>%s</td><td class=num>%s</td><td>%s</td></tr>"
                        % (_pl(f), _e(f.get("team") or "FA"), _e(f["pos"]), _e(f["pos"]), _e(f.get("age") or ""),
                           _n(f["value"]), _n(f["now"]), _signed(f["starts"]) if f["starts"] else "<span class=faint>no</span>")
                        for f in m["free_agents"]))
    extra = []
    if m.get("trending_unvalued"):
        extra.append("Trending on Sleeper, not yet valued: " + ", ".join(
            "%s (%s %s)" % (_pl(t), _e(t.get("team")), _e(t["pos"])) for t in m["trending_unvalued"]))
    if m.get("skipped_injured"):
        n = m["skipped_injured"]
        extra.append("Left out: %d free agent%s done for the season." % (n, "" if n == 1 else "s"))
    if m.get("cheapest"):
        extra.append("Cheapest to lose: " + ", ".join(
            "%s (%s%s)" % (_pl(c), _n(c["value"]), ", starting" if c.get("starter") else "") for c in m["cheapest"]))
    return line + "<div class=moves>%s</div>%s%s" % ("".join(rows), fa,
                                                     "".join("<p class='small muted'>%s</p>" % x for x in extra))


def _teams(rows: List[Dict]) -> str:
    return ("<div class='card tbl'><table><thead><tr><th class=num>#</th><th>Team</th><th>Record</th><th class=num>Points</th>"
            "<th>Lineup strength</th><th>Mode</th><th>Thinnest at</th></tr></thead><tbody>%s</tbody></table></div>"
            % "".join("<tr%s><td class=num>%d</td><td><b>%s</b>%s</td><td>%s</td><td class=num>%s</td>"
                      "<td><span class=meter><span style='width:%.0f%%'></span></span><span class=faint>%s</span></td>"
                      "<td>%s</td><td>%s</td></tr>"
                      % (" class=me" if t["me"] else "", t["rank"], _e(t["name"]), "<span class=tag>you</span>" if t["me"] else "",
                         _e(t["record"]), _n(t["pf"]), 100 * t["lineup_share"], _n(t["lineup"]), _mode_chip(t["mode"]),
                         "<span class='pos %s'>%s</span>" % (_e(t["thin"]), _e(t["thin"])) if t["thin"] else "<span class=faint>—</span>")
                      for t in rows))


def _managers(rows: List[Dict]) -> str:
    return ("<div class='card tbl'><table><thead><tr><th>Manager</th><th class=num>Accepted</th><th class=num>Proposed</th>"
            "<th>Softness</th><th>Opening lean</th><th>Tendency</th></tr></thead><tbody>%s</tbody></table></div>"
            "<p class='small faint'>Only completed trades are visible — rejected offers never show up — so these are counts, not acceptance rates. "
            "Opening lean is how far an opening offer tilts your way.</p>"
            % "".join("<tr%s><td><b>%s</b>%s</td><td class=num>%d</td><td class=num>%d</td>"
                      "<td><span class=meter><span style='width:%.0f%%'></span></span><span class=faint>%.2f</span></td><td>%s</td>"
                      "<td class=muted style='white-space:normal;min-width:220px'>%s</td></tr>"
                      % (" class=me" if m["me"] else "", _e(m["name"]), "<span class=tag>you</span>" if m["me"] else "",
                         m["accepted"], m["proposed"], 100 * m["softness"], m["softness"],
                         "%d%%" % m["edge"] if m["edge"] is not None else "<span class=faint>needs a clear win for them</span>",
                         _e(m["tendency"] or "—"))
                      for m in rows))


def _picks(picks: List[Dict]) -> str:
    by: Dict[str, List[Dict]] = {}
    for p in picks:
        by.setdefault(p["season"], []).append(p)
    return "<div class='card picks'>%s</div>" % "".join(
        "<div class=pyear><span class=yr>%s</span>%s</div>" % (_e(y), "".join(
            "<span class='pk r%d'>%s%s <small>%s</small></span>" % (
                p["round"], _e(p["name"].split(" ", 1)[1] if " " in p["name"] else p["name"]),
                " <small>via %s</small>" % _e(p["via"]) if p.get("via") else "", _n(p["value"]))
            for p in ps)) for y, ps in sorted(by.items()))


def render_view(v: Dict) -> str:
    out = []
    dl = v.get("deadline")
    if dl and dl["state"] != "closed" and dl.get("weeks_left", 9) <= 1:
        out.append("<div class=banner><span aria-hidden=true>⏱</span><div><b>Trade deadline: week %d.</b> "
                   "It's week %d — make trades now or wait for the offseason.</div></div>" % (dl["week"], v["week"]))
    elif dl and dl["state"] == "closed":
        out.append("<div class=banner><span aria-hidden=true>ℹ︎</span><div><b>Trades are closed</b> (deadline week %d). "
                   "These ideas are for the offseason.</div></div>" % dl["week"])
    for w in v.get("warnings") or []:
        out.append("<div class='banner err'><span aria-hidden=true>⚠︎</span><div>%s</div></div>" % _e(w))
    out.append(_tiles(v))
    rv = v.get("review") or {}
    if rv.get("error"):
        out.append("<div class='banner err'><span aria-hidden=true>⚠︎</span><div><b>AI review unavailable</b> — "
                   "these ideas are unreviewed. %s</div></div>" % _e(rv["error"]))
    elif rv.get("overview"):
        out.append("<div class='card overview'><span class=rv-tag>AI review</span><p>%s</p></div>" % _e(rv["overview"]))
    trades = v.get("trades") or []
    kept = [t for t in trades if (t.get("review") or {}).get("verdict") != "pass"]
    dropped = [t for t in trades if (t.get("review") or {}).get("verdict") == "pass"]
    passed = ""
    if dropped:
        passed = ("<details class='card passed'><summary>The reviewer passed on %d idea%s</summary>%s</details>" % (
            len(dropped), "" if len(dropped) == 1 else "s",
            "".join(_trade(i, t) for i, t in enumerate(dropped, len(kept) + 1))))
    out.append("<section><div class=sec-head><h2>Trade targets</h2><span class='small faint'>%d idea%s, best first</span></div>"
               "<div class=trades>%s</div>%s</section>" % (
                   len(kept), "" if len(kept) == 1 else "s",
                   "".join(_trade(i, t) for i, t in enumerate(kept, 1)) or
                   "<div class='card trade muted'>No trade found that helps you and that the other manager plausibly accepts.</div>",
                   passed))
    out.append("<section><div class=sec-head><h2>Roster moves</h2></div>%s</section>" % _moves(v["moves"]))
    out.append("<section><div class=sec-head><h2>League</h2><span class='small faint'>projected finish</span></div>%s</section>" % _teams(v["teams"]))
    out.append("<section><div class=sec-head><h2>Managers</h2><span class='small faint'>most likely to accept first</span></div>%s</section>" % _managers(v["managers"]))
    if v.get("protected"):
        out.append("<section><div class=sec-head><h2>Protected</h2><span class='small faint'>never offered for veterans or at a loss</span></div>"
                   "<div class='card prot'>%s</div></section>" % "".join(
                       "<span class=pk><span class='pos %s'>%s</span> %s <small>%s · %s</small></span>" % (
                           _e(p["pos"]), _e(p["pos"]), _pl(p), _e(p.get("age") or "?"),
                           "untouchable" if p.get("untouchable") else "young &amp; rising")
                       for p in v["protected"]))
    out.append("<section><div class=sec-head><h2>Your picks</h2></div>%s</section>" % _picks(v["picks"]))
    out.append("<footer>Values are FantasyCalc dynasty values; trades compare value over replacement (%s, the median of the best free agents). "
               "Your side is age-adjusted on a dynasty curve (peak QB 30, RB 25, WR 27, TE 28) and no idea may lose long-term value. “Your lineup” is the change in your best starting lineup’s rest-of-season value. Past trades are judged at today’s values. %s</footer>"
               % (_n(v.get("replacement") or 0), _e(v.get("attribution"))))
    return "\n".join(out)
