"""Render the profiling database as one self-contained HTML report (Plotly) plus CSV/Parquet exports."""

from __future__ import annotations

import html
import json
from pathlib import Path

import duckdb
import plotly.graph_objects as go
from plotly.offline import get_plotlyjs

# Categorical slots 1-4 of the reference palette (blue, orange, aqua, yellow), light and dark steps.
SERIES = {
    "light": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"],
    "dark": ["#3987e5", "#d95926", "#199e70", "#c98500"],
}
INK = {"light": {"text": "#0b0b0b", "muted": "#52514e", "grid": "#e4e3df"},
       "dark": {"text": "#ffffff", "muted": "#c3c2b7", "grid": "#34342f"}}
VIEWS = ["v_summary", "v_providers", "v_steps", "v_steps_actor", "v_steps_token_kind", "v_step_medians",
         "v_session_totals", "v_session_steps", "v_agents", "v_agent_types", "v_images"]


def _rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[dict]:
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


SHORT = {
    "Setup & investigation (until draft PR)": "Setup & investigation",
    "Review (quick, mid-work)": "Review quick",
    "Review (full, pre-publish)": "Review full",
    "Publish, CI & follow-ups": "Publish & after",
}


def _srows(con: duckdb.DuckDBPyConnection, sql: str) -> list[dict]:
    """Like _rows, with short step labels so charts keep their plot area on narrow screens."""
    rows = _rows(con, sql)
    for r in rows:
        if "label" in r:
            r["label"] = SHORT.get(r["label"], r["label"])
    return rows


def _layout(fig: go.Figure, height: int, **kw) -> go.Figure:
    light = INK["light"]
    fig.update_layout(
        height=height, margin=dict(l=10, r=30, t=10, b=40),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="system-ui, -apple-system, Segoe UI, sans-serif", size=12, color=light["text"]),
        legend=dict(orientation="h", y=-0.18, x=0, font=dict(color=light["text"])),
        hoverlabel=dict(font_size=13), **kw,
    )
    fig.update_xaxes(gridcolor=light["grid"], zeroline=False, linecolor=light["grid"], ticks="")
    fig.update_yaxes(gridcolor="rgba(0,0,0,0)", linecolor=light["grid"], ticks="", automargin=True)
    return fig


def _bar(name: str, slot: int, **kw) -> go.Bar:
    return go.Bar(name=name, meta={"slot": slot}, marker=dict(color=SERIES["light"][slot], line=dict(color="rgba(0,0,0,0)", width=0)), **kw)


def fig_cost_share(con) -> go.Figure:
    steps = _srows(con, "SELECT label, pct_cost, cost FROM v_steps ORDER BY ord")
    by_actor = {(r["label"], r["actor_kind"]): r for r in _srows(con, "SELECT label, actor_kind, cost, pct_cost FROM v_steps_actor")}
    labels = [s["label"] for s in steps]
    fig = go.Figure()
    for slot, kind, name in [(0, "main", "Main thread"), (1, "subagent", "Subagents")]:
        fig.add_trace(_bar(
            name, slot, y=labels, orientation="h",
            x=[by_actor.get((lbl, kind), {}).get("pct_cost") or 0 for lbl in labels],
            customdata=[[by_actor.get((lbl, kind), {}).get("cost") or 0] for lbl in labels],
            hovertemplate="%{y}<br>" + name + ": %{x:.1f}% of cost ($%{customdata[0]:.2f})<extra></extra>",
        ))
    fig.add_trace(go.Scatter(
        x=[s["pct_cost"] for s in steps], y=labels, mode="text", showlegend=False, hoverinfo="skip",
        text=[f"{s['pct_cost']:.1f}%" for s in steps], textposition="middle right", cliponaxis=False,
        textfont=dict(color=INK["light"]["text"]), meta={"role": "label"},
    ))
    fig.update_layout(barmode="stack", bargap=0.35)
    fig.update_yaxes(autorange="reversed")
    fig.update_xaxes(title="share of total workflow cost (%)", rangemode="tozero")
    return _layout(fig, 430)


def fig_metrics(con) -> go.Figure:
    steps = _srows(con, "SELECT label, pct_tokens, pct_fresh_tokens, pct_cost FROM v_steps ORDER BY ord")
    labels = [s["label"] for s in steps]
    fig = go.Figure()
    for slot, key, name in [(0, "pct_cost", "Cost (USD-weighted)"), (1, "pct_tokens", "All tokens"),
                            (2, "pct_fresh_tokens", "Fresh tokens (no cache reads)")]:
        fig.add_trace(_bar(name, slot, y=labels, x=[s[key] for s in steps], orientation="h",
                           hovertemplate="%{y}<br>" + name + ": %{x:.1f}%<extra></extra>"))
    fig.update_layout(barmode="group", bargap=0.3, bargroupgap=0.08)
    fig.update_yaxes(autorange="reversed")
    fig.update_xaxes(title="share of workflow total (%)", rangemode="tozero")
    return _layout(fig, 640)


def fig_token_kinds(con) -> go.Figure:
    rows = _srows(con, "SELECT label, kind, cost FROM v_steps_token_kind ORDER BY ord")
    labels = list(dict.fromkeys(r["label"] for r in rows))
    totals = {lbl: sum(r["cost"] for r in rows if r["label"] == lbl) or 1 for lbl in labels}
    fig = go.Figure()
    for slot, kind in enumerate(["cache read", "cache write", "uncached input", "output"]):
        vals = {r["label"]: r["cost"] for r in rows if r["kind"] == kind}
        fig.add_trace(_bar(
            kind, slot, y=labels, orientation="h", x=[100 * vals.get(lbl, 0) / totals[lbl] for lbl in labels],
            customdata=[[vals.get(lbl, 0)] for lbl in labels],
            hovertemplate="%{y}<br>" + kind + ": %{x:.0f}% of step cost ($%{customdata[0]:.2f})<extra></extra>"))
    fig.update_layout(barmode="stack", bargap=0.35)
    fig.update_yaxes(autorange="reversed")
    fig.update_xaxes(title="composition of the step's cost (%)", range=[0, 100])
    return _layout(fig, 430)


def fig_session_spread(con) -> go.Figure:
    rows = _srows(con, "SELECT label, pct_of_session, title FROM v_session_steps ORDER BY ord")
    labels = list(dict.fromkeys(r["label"] for r in rows))
    all_sessions = {r["title"] for r in rows}
    fig = go.Figure()
    for lbl in labels:
        vals = {r["title"]: r["pct_of_session"] for r in rows if r["label"] == lbl}
        xs = [vals.get(t, 0.0) for t in all_sessions]
        fig.add_trace(go.Box(
            x=xs, y=[lbl] * len(xs), name=lbl, orientation="h", boxpoints="all", jitter=0.5, pointpos=0,
            marker=dict(color=SERIES["light"][0], size=4, opacity=0.55), line=dict(color=SERIES["light"][0], width=1.5),
            fillcolor="rgba(42,120,214,0.12)", meta={"slot": 0}, showlegend=False,
            text=list(all_sessions), hovertemplate="%{y}<br>%{x:.1f}% of this session<br>%{text}<extra></extra>"))
    fig.update_yaxes(autorange="reversed")
    fig.update_xaxes(title="share of one session's cost (%), each dot is a session", rangemode="tozero")
    return _layout(fig, 560)


def fig_top_agents(con, n: int = 12) -> go.Figure:
    rows = _rows(con, f"SELECT description, agent_type, dominant_phase, msgs, cost FROM v_agents ORDER BY cost DESC LIMIT {n}")
    labels = [(r["description"] or r["agent_type"] or "?")[:46] for r in rows]
    fig = go.Figure(_bar(
        "Subagent cost", 0, y=labels, x=[r["cost"] for r in rows], orientation="h", showlegend=False,
        customdata=[[r["agent_type"], r["dominant_phase"], r["msgs"]] for r in rows],
        hovertemplate="%{y}<br>$%{x:.2f}, %{customdata[2]} turns<br>%{customdata[0]}, mostly during: %{customdata[1]}<extra></extra>"))
    fig.update_layout(bargap=0.4)
    fig.update_yaxes(autorange="reversed")
    fig.update_xaxes(title="API-equivalent cost (USD)", rangemode="tozero")
    return _layout(fig, 400)


def fig_images(con) -> go.Figure:
    rows = _srows(con, "SELECT label, img_carry_cost, pct_of_step_cost FROM v_images ORDER BY ord")
    fig = go.Figure(_bar(
        "Image carry", 0, y=[r["label"] for r in rows], x=[r["pct_of_step_cost"] or 0 for r in rows], orientation="h",
        showlegend=False, customdata=[[r["img_carry_cost"] or 0] for r in rows],
        hovertemplate="%{y}<br>%{x:.1f}% of the step's cost ($%{customdata[0]:.2f})<extra></extra>"))
    fig.update_layout(bargap=0.4)
    fig.update_yaxes(autorange="reversed")
    fig.update_xaxes(title="estimated share of step cost spent re-reading screenshots (%)", rangemode="tozero")
    return _layout(fig, 400)


def findings(con) -> list[str]:
    s = _rows(con, "SELECT * FROM v_summary")[0]
    steps = _rows(con, "SELECT * FROM v_steps ORDER BY pct_cost DESC")
    by_phase = {r["phase"]: r for r in steps}

    def pct(*phases: str) -> float:
        return sum(by_phase[p]["pct_cost"] for p in phases if p in by_phase)

    img = _rows(con, "SELECT sum(img_carry_cost) AS c, sum(cost) AS t FROM v_images")[0]
    img_stats = _rows(con, "SELECT count(*) AS n, coalesce(sum(est_tokens), 0) AS tok FROM images "
                           "WHERE thread_id IN (SELECT thread_id FROM f)")[0]
    top = steps[:3]
    out = [
        f"The three most expensive steps are {', '.join(f'<b>{html.escape(t['label'])}</b> ({t['pct_cost']:.0f}%)' for t in top)}.",
        f"All reviews together (plan review, quick, full, design review) take <b>{pct('plan_review', 'review_quick', 'review_full', 'design_review'):.0f}%</b> of the cost; "
        f"the pre-publish full review alone <b>{pct('review_full'):.0f}%</b> and the design review <b>{pct('design_review'):.1f}%</b>.",
        f"Screenshots as a step take <b>{pct('screenshots'):.0f}%</b>, but the {img_stats['n']} images themselves (≈{img_stats['tok']:,} tokens in total) "
        f"only cause an estimated <b>{100 * (img['c'] or 0) / (img['t'] or 1):.1f}%</b> of cost by being re-read from context; the step is expensive because of the browser loop around them, not the pixels.",
        f"Subagents account for <b>{s['pct_cost_subagents']:.0f}%</b> of the workflow cost.",
    ]
    ctx = max(steps, key=lambda r: r["avg_context_tok"])
    out.append(f"Every turn re-reads the whole context (average {ctx['avg_context_tok'] / 1000:.0f}K tokens in the heaviest step), so a step's cost is mostly its <i>number of turns</i> times that context.")
    return out


def _unpriced_note(summary: dict) -> str:
    if not summary["unpriced_msgs"]:
        return ""
    return (f'<p class="note"><b>{summary["unpriced_msgs"]:,} turns of {html.escape(summary["unpriced_models"])} have no price</b>, '
            "so they are missing from every cost number here (their tokens are counted). "
            "Pass <code>--price MODEL=IN,OUT,CACHED</code> (USD per million tokens) to include them.</p>")


def _table(con) -> str:
    rows = _rows(con, "SELECT * FROM v_steps ORDER BY ord")
    head = ["Step", "Turns", "Sessions", "Avg context (K)", "USD", "% cost", "% all tokens", "% fresh tokens", "% via subagents"]
    body = "".join(
        f"<tr><th scope=row>{html.escape(r['label'])}</th><td>{r['msgs']:,}</td><td>{r['sessions']}</td>"
        f"<td>{r['avg_context_tok'] / 1000:.0f}</td><td>{r['cost']:.2f}</td><td>{r['pct_cost']:.1f}</td>"
        f"<td>{r['pct_tokens']:.1f}</td><td>{r['pct_fresh_tokens']:.1f}</td>"
        f"<td>{'' if r['pct_of_step_via_subagents'] is None else format(r['pct_of_step_via_subagents'], '.0f')}</td></tr>"
        for r in rows)
    return "<table><thead><tr>" + "".join(f"<th>{h}</th>" for h in head) + f"</tr></thead><tbody>{body}</tbody></table>"


def _agent_table(con) -> str:
    rows = _rows(con, "SELECT * FROM v_agent_types ORDER BY cost DESC LIMIT 15")
    body = "".join(
        f"<tr><th scope=row>{html.escape(r['agent_type'])}</th><td>{html.escape(r['phase'] or '')}</td><td>{r['agents']}</td>"
        f"<td>{r['cost']:.2f}</td><td>{r['avg_cost_per_agent']:.2f}</td><td>{r['pct_cost_of_workflow']:.1f}</td></tr>" for r in rows)
    return ("<table><thead><tr><th>Agent type</th><th>Mostly during</th><th>Agents</th><th>USD</th><th>USD / agent</th>"
            f"<th>% of workflow</th></tr></thead><tbody>{body}</tbody></table>")


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Workflow Profile</title>
<style>
:root{color-scheme:light;--bg:#fcfcfb;--surface:#ffffff;--text:#0b0b0b;--muted:#52514e;--line:#e4e3df;--accent:#2a78d6}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--bg:#1a1a19;--surface:#222221;--text:#fff;--muted:#c3c2b7;--line:#34342f;--accent:#3987e5}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1040px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:40px 0 4px}p.sub{color:var(--muted);margin:0 0 16px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:20px 0}
.kpi{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.kpi b{display:block;font-size:22px}.kpi span{color:var(--muted);font-size:13px}
ul.find{padding-left:20px}ul.find li{margin:6px 0}
.card{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:12px 12px 4px}
table{border-collapse:collapse;width:100%;font-size:13.5px;font-variant-numeric:tabular-nums}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right}th:first-child,th[scope=row]{text-align:left;font-weight:500}
thead th{color:var(--muted);font-weight:500}.scroll{overflow-x:auto}
details{margin-top:12px}summary{cursor:pointer;color:var(--muted)}
.note{color:var(--muted);font-size:13.5px}
</style></head><body><main>
<h1>Where do the tokens go in <code>/dev-workflow</code>?</h1>
<p class="sub">__RANGE__ · estimates from local Claude Code and codex logs · cost = API list price equivalent (not your actual bill)</p>
<div class="kpis">__KPIS__</div>
__UNPRICED__
<ul class="find">__FINDINGS__</ul>

<h2>Share of cost per workflow step</h2>
<p class="sub">Steps in workflow order. Split by who spent it: the main conversation or subagents it spawned.</p>
<div class="card">__FIG_COST__</div>

<h2>Does the metric change the story?</h2>
<p class="sub">Raw token counts are dominated by cache reads (cheap). Cost weighs every token type by its price.</p>
<div class="card">__FIG_METRICS__</div>

<h2>What kind of tokens does each step spend?</h2>
<div class="card">__FIG_KINDS__</div>

<h2>Typical session, not just the total</h2>
<p class="sub">A few huge sessions can dominate the totals. Each dot is one session's share spent in that step.</p>
<div class="card">__FIG_SPREAD__</div>

<h2>Are screenshots the problem?</h2>
<p class="sub">Estimated cost of re-reading screenshots that sit in the context of every later turn (image tokens ≈ width×height/750 from the PNG header).</p>
<div class="card">__FIG_IMG__</div>

<h2>Most expensive subagents</h2>
<div class="card">__FIG_AGENTS__</div>
<div class="scroll" style="margin-top:12px">__AGENT_TABLE__</div>

<h2>Table view</h2>
<div class="scroll">__TABLE__</div>

<h2>How to read this</h2>
<p class="note">Each assistant message is assigned to a workflow step by markers in the tool calls (Skill calls such as bms-code-review or design-review, <code>bdt</code> commands, edits, pytest, screenshot tooling, image reads). A step is sticky until the next marker, except test runs, which only count for the turn that runs them. Subagents inherit the step of the message that spawned them, unless they run the whole workflow themselves. Rules live in <code>sql/03_markers.sql</code> and <code>pipeline.py</code>. Attribution of turns that sit between two markers is a judgement call, treat differences of a few percent as noise. "Publish, CI &amp; follow-ups" includes everything after the PR goes up (git, <code>az repos</code>, merges, fixes that are not edits).</p>
</main>
<script>
const SERIES = __SERIES__, INK = __INK__;
function rgba(h,a){const n=parseInt(h.slice(1),16);return `rgba(${n>>16},${(n>>8)&255},${n&255},${a})`}
function theme(){
  const dark = matchMedia('(prefers-color-scheme: dark)').matches, k = dark?'dark':'light', t = INK[k];
  document.querySelectorAll('.plotly-graph-div').forEach(gd=>{
    Plotly.relayout(gd,{'font.color':t.text,'legend.font.color':t.text,'xaxis.gridcolor':t.grid,'xaxis.linecolor':t.grid,'yaxis.linecolor':t.grid});
    (gd.data||[]).forEach((tr,i)=>{
      const slot = tr.meta && tr.meta.slot;
      if(slot!==undefined){
        const u = {'marker.color':SERIES[k][slot]};
        if(tr.type==='box'){u['line.color']=SERIES[k][slot];u['fillcolor']=rgba(SERIES[k][slot],0.14)}
        Plotly.restyle(gd,u,[i]);
      } else if(tr.meta && tr.meta.role==='label'){Plotly.restyle(gd,{'textfont.color':t.text},[i])}
    });
  });
}
addEventListener('load',theme);matchMedia('(prefers-color-scheme: dark)').addEventListener('change',theme);
</script></body></html>"""


def write_report(con: duckdb.DuckDBPyConnection, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_dir = out_dir / "csv"
    csv_dir.mkdir(exist_ok=True)
    for view in VIEWS:
        con.execute(f"COPY (SELECT * FROM {view}) TO '{csv_dir / (view + '.csv')}' (HEADER)")
    con.execute(f"COPY facts TO '{out_dir / 'facts.parquet'}' (FORMAT PARQUET)")

    s = _rows(con, "SELECT * FROM v_summary")[0]
    providers = _rows(con, "SELECT * FROM v_providers")
    kpis = [
        (f"{s['sessions']}", "sessions in scope"),
        (f"{s['threads']}", "threads (main + subagents)"),
        (f"${s['cost']:,.0f}", "API-equivalent cost"),
        (f"{s['tokens'] / 1e9:.2f} B", "tokens processed"),
        (f"{s['fresh_tokens'] / 1e6:.0f} M", "fresh tokens (no cache reads)"),
        (f"{s['pct_cost_subagents']:.0f}%", "of cost in subagents"),
        *((f"{r['pct_tokens']:.1f}%", f"of tokens in {r['provider']}") for r in providers if r["provider"] != "claude"),
    ]
    figs = {
        "COST": fig_cost_share(con), "METRICS": fig_metrics(con), "KINDS": fig_token_kinds(con),
        "SPREAD": fig_session_spread(con), "IMG": fig_images(con), "AGENTS": fig_top_agents(con),
    }
    page = PAGE
    plot_cfg = {"displayModeBar": False, "responsive": True}
    for i, (key, fig) in enumerate(figs.items()):
        div = fig.to_html(full_html=False, include_plotlyjs=False, config=plot_cfg, div_id=f"fig_{key.lower()}")
        page = page.replace(f"__FIG_{key}__", div)
    page = (page.replace("__KPIS__", "".join(f'<div class="kpi"><b>{v}</b><span>{html.escape(k)}</span></div>' for v, k in kpis))
            .replace("__FINDINGS__", "".join(f"<li>{f}</li>" for f in findings(con)))
            .replace("__UNPRICED__", _unpriced_note(s))
            .replace("__RANGE__", f"{s['first_ts']:%Y-%m-%d} to {s['last_ts']:%Y-%m-%d}")
            .replace("__TABLE__", _table(con)).replace("__AGENT_TABLE__", _agent_table(con))
            .replace("__SERIES__", json.dumps(SERIES)).replace("__INK__", json.dumps(INK)))
    page = page.replace("</head>", f"<script>{get_plotlyjs()}</script></head>", 1)
    out = out_dir / "report.html"
    out.write_text(page, encoding="utf-8")
    return out
