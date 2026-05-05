#!/usr/bin/env python3
"""Generate all README SVG assets (light + dark variants) with CSS animations."""

from __future__ import annotations
from pathlib import Path

OUT = Path(__file__).parent.parent / "docs" / "readme"
OUT.mkdir(parents=True, exist_ok=True)

# ── Color palettes ─────────────────────────────────────────────────────────

LIGHT = {
    "bg": "#ffffff",
    "bg2": "#f6f8fa",
    "bg3": "#eef1f5",
    "text": "#1f2328",
    "text2": "#656d76",
    "border": "#d1d9e0",
    "primary": "#4F46E5",
    "free": "#10B981",
    "budget": "#F59E0B",
    "premium": "#EF4444",
    "savings": "#06B6D4",
    "accent": "#8B5CF6",
}

DARK = {
    "bg": "#0d1117",
    "bg2": "#161b22",
    "bg3": "#21262d",
    "text": "#e6edf3",
    "text2": "#8b949e",
    "border": "#30363d",
    "primary": "#818CF8",
    "free": "#34D399",
    "budget": "#FBBF24",
    "premium": "#F87171",
    "savings": "#22D3EE",
    "accent": "#A78BFA",
}


def write_svg(name: str, content_fn):
    for mode, colors in [("light", LIGHT), ("dark", DARK)]:
        svg = content_fn(colors, mode)
        path = OUT / f"{name}-{mode}.svg"
        path.write_text(svg)
        print(f"  wrote {path.name}")


# ── 1. Hero Banner ─────────────────────────────────────────────────────────

def hero_banner(c, mode):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 320" fill="none">
  <style>
    @keyframes flowDot {{
      0% {{ opacity: 0; transform: translateX(-20px); }}
      20% {{ opacity: 1; }}
      80% {{ opacity: 1; }}
      100% {{ opacity: 0; transform: translateX(20px); }}
    }}
    @keyframes pulse {{
      0%, 100% {{ opacity: 0.12; }}
      50% {{ opacity: 0.22; }}
    }}
    @keyframes glow {{
      0%, 100% {{ filter: drop-shadow(0 0 2px {c['primary']}00); }}
      50% {{ filter: drop-shadow(0 0 8px {c['primary']}66); }}
    }}
    @keyframes tierSlide {{
      0% {{ opacity: 0; transform: translateX(-10px); }}
      100% {{ opacity: 1; transform: translateX(0); }}
    }}
    @keyframes costPulse {{
      0%, 100% {{ opacity: 0.6; }}
      50% {{ opacity: 1; }}
    }}
    @keyframes pillFloat {{
      0%, 100% {{ transform: translateY(0); }}
      50% {{ transform: translateY(-3px); }}
    }}
    .flow-dot {{ animation: flowDot 2s ease-in-out infinite; }}
    .flow-dot-2 {{ animation: flowDot 2s ease-in-out 0.7s infinite; }}
    .flow-dot-3 {{ animation: flowDot 2s ease-in-out 1.4s infinite; }}
    .classifier-bg {{ animation: pulse 3s ease-in-out infinite; }}
    .classifier-box {{ animation: glow 3s ease-in-out infinite; }}
    .tier-free {{ animation: tierSlide 0.6s ease-out 0.3s both; }}
    .tier-budget {{ animation: tierSlide 0.6s ease-out 0.5s both; }}
    .tier-premium {{ animation: tierSlide 0.6s ease-out 0.7s both; }}
    .cost-bar {{ animation: costPulse 4s ease-in-out infinite; }}
    .pill-1 {{ animation: pillFloat 3s ease-in-out infinite; }}
    .pill-2 {{ animation: pillFloat 3s ease-in-out 0.5s infinite; }}
    .pill-3 {{ animation: pillFloat 3s ease-in-out 1s infinite; }}
  </style>

  <defs>
    <linearGradient id="heroBg" x1="0" y1="0" x2="900" y2="320" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="{c['bg']}"/>
      <stop offset="100%" stop-color="{c['bg2']}"/>
    </linearGradient>
    <linearGradient id="routeGrad" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{c['free']}"/>
      <stop offset="50%" stop-color="{c['budget']}"/>
      <stop offset="100%" stop-color="{c['premium']}"/>
    </linearGradient>
    <marker id="arrowHead" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="{c['primary']}"/></marker>
    <marker id="arrowFree" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="{c['free']}"/></marker>
    <marker id="arrowBudget" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="{c['budget']}"/></marker>
    <marker id="arrowPremium" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="{c['premium']}"/></marker>
  </defs>

  <rect width="900" height="320" rx="16" fill="url(#heroBg)"/>
  <rect x="1" y="1" width="898" height="318" rx="15" fill="none" stroke="{c['border']}" stroke-width="1" opacity="0.5"/>

  <!-- Title -->
  <text x="450" y="62" text-anchor="middle" font-family="system-ui, -apple-system, sans-serif" font-size="42" font-weight="800" fill="{c['text']}" letter-spacing="-1">LLM Router</text>
  <text x="450" y="92" text-anchor="middle" font-family="system-ui, sans-serif" font-size="16" fill="{c['text2']}">Route every AI call to the cheapest model that can do the job well.</text>

  <!-- User prompt box -->
  <rect x="60" y="140" width="140" height="56" rx="10" fill="{c['bg2']}" stroke="{c['border']}"/>
  <text x="130" y="164" text-anchor="middle" font-family="system-ui, sans-serif" font-size="11" fill="{c['text2']}">User Prompt</text>
  <text x="130" y="182" text-anchor="middle" font-family="monospace" font-size="10" fill="{c['text']}">"fix the auth bug"</text>

  <!-- Arrow 1 with flowing dots -->
  <line x1="200" y1="168" x2="260" y2="168" stroke="{c['primary']}" stroke-width="2" stroke-opacity="0.3"/>
  <circle class="flow-dot" cx="230" cy="168" r="3" fill="{c['primary']}"/>
  <circle class="flow-dot-2" cx="230" cy="168" r="3" fill="{c['primary']}"/>

  <!-- Classifier box with glow -->
  <rect class="classifier-bg" x="270" y="132" width="160" height="72" rx="12" fill="{c['primary']}"/>
  <rect class="classifier-box" x="270" y="132" width="160" height="72" rx="12" fill="none" stroke="{c['primary']}" stroke-width="1.5"/>
  <text x="350" y="157" text-anchor="middle" font-family="system-ui, sans-serif" font-size="12" font-weight="700" fill="{c['primary']}">Complexity</text>
  <text x="350" y="174" text-anchor="middle" font-family="system-ui, sans-serif" font-size="12" font-weight="700" fill="{c['primary']}">Classifier</text>
  <text x="350" y="194" text-anchor="middle" font-family="system-ui, sans-serif" font-size="10" fill="{c['text2']}">heuristic + local LLM</text>

  <!-- Branching arrows with flowing dots -->
  <line x1="430" y1="148" x2="520" y2="148" stroke="{c['free']}" stroke-width="2" stroke-opacity="0.3" marker-end="url(#arrowFree)"/>
  <circle class="flow-dot" cx="475" cy="148" r="3" fill="{c['free']}"/>

  <line x1="430" y1="168" x2="520" y2="208" stroke="{c['budget']}" stroke-width="2" stroke-opacity="0.3" marker-end="url(#arrowBudget)"/>
  <circle class="flow-dot-2" cx="475" cy="188" r="3" fill="{c['budget']}"/>

  <line x1="430" y1="188" x2="520" y2="268" stroke="{c['premium']}" stroke-width="2" stroke-opacity="0.3" marker-end="url(#arrowPremium)"/>
  <circle class="flow-dot-3" cx="475" cy="228" r="3" fill="{c['premium']}"/>

  <!-- Free tier (animated slide-in) -->
  <g class="tier-free">
    <rect x="530" y="126" width="200" height="44" rx="8" fill="{c['free']}" opacity="0.15" stroke="{c['free']}" stroke-width="1.5"/>
    <circle cx="548" cy="148" r="6" fill="{c['free']}">
      <animate attributeName="r" values="5;7;5" dur="2s" repeatCount="indefinite"/>
    </circle>
    <text x="562" y="144" font-family="system-ui, sans-serif" font-size="12" font-weight="700" fill="{c['free']}">Simple</text>
    <text x="562" y="160" font-family="system-ui, sans-serif" font-size="10" fill="{c['text2']}">Ollama / Haiku / Gemini Flash</text>
    <text x="740" y="152" text-anchor="end" font-family="system-ui, sans-serif" font-size="11" font-weight="700" fill="{c['free']}">FREE</text>
  </g>

  <!-- Budget tier -->
  <g class="tier-budget">
    <rect x="530" y="186" width="200" height="44" rx="8" fill="{c['budget']}" opacity="0.15" stroke="{c['budget']}" stroke-width="1.5"/>
    <circle cx="548" cy="208" r="6" fill="{c['budget']}">
      <animate attributeName="r" values="5;7;5" dur="2.5s" repeatCount="indefinite"/>
    </circle>
    <text x="562" y="204" font-family="system-ui, sans-serif" font-size="12" font-weight="700" fill="{c['budget']}">Moderate</text>
    <text x="562" y="220" font-family="system-ui, sans-serif" font-size="10" fill="{c['text2']}">GPT-4o / Gemini Pro</text>
    <text x="740" y="212" text-anchor="end" font-family="system-ui, sans-serif" font-size="11" font-weight="700" fill="{c['budget']}">$0.01</text>
  </g>

  <!-- Premium tier -->
  <g class="tier-premium">
    <rect x="530" y="246" width="200" height="44" rx="8" fill="{c['premium']}" opacity="0.15" stroke="{c['premium']}" stroke-width="1.5"/>
    <circle cx="548" cy="268" r="6" fill="{c['premium']}">
      <animate attributeName="r" values="5;7;5" dur="3s" repeatCount="indefinite"/>
    </circle>
    <text x="562" y="264" font-family="system-ui, sans-serif" font-size="12" font-weight="700" fill="{c['premium']}">Complex</text>
    <text x="562" y="280" font-family="system-ui, sans-serif" font-size="10" fill="{c['text2']}">Claude Opus / o3</text>
    <text x="740" y="272" text-anchor="end" font-family="system-ui, sans-serif" font-size="11" font-weight="700" fill="{c['premium']}">$$$</text>
  </g>

  <!-- Cost gradient bar -->
  <rect class="cost-bar" x="770" y="130" width="4" height="156" rx="2" fill="url(#routeGrad)"/>
  <text x="786" y="150" font-family="system-ui, sans-serif" font-size="12" fill="{c['free']}">$</text>
  <text x="786" y="282" font-family="system-ui, sans-serif" font-size="12" fill="{c['premium']}">$$$$</text>

  <!-- Bottom stat pills with float animation -->
  <g class="pill-1">
    <rect x="145" y="260" width="120" height="28" rx="14" fill="{c['primary']}" opacity="0.1" stroke="{c['primary']}" stroke-width="0.5"/>
    <text x="205" y="278" text-anchor="middle" font-family="system-ui, sans-serif" font-size="11" font-weight="600" fill="{c['primary']}">48 MCP Tools</text>
  </g>
  <g class="pill-2">
    <rect x="285" y="260" width="120" height="28" rx="14" fill="{c['savings']}" opacity="0.1" stroke="{c['savings']}" stroke-width="0.5"/>
    <text x="345" y="278" text-anchor="middle" font-family="system-ui, sans-serif" font-size="11" font-weight="600" fill="{c['savings']}">20+ Providers</text>
  </g>
  <g class="pill-3">
    <rect x="425" y="260" width="80" height="28" rx="14" fill="{c['free']}" opacity="0.1" stroke="{c['free']}" stroke-width="0.5"/>
    <text x="465" y="278" text-anchor="middle" font-family="system-ui, sans-serif" font-size="11" font-weight="600" fill="{c['free']}">87% saved</text>
  </g>
</svg>'''


# ── 2. Why Route? Benefits Panel ───────────────────────────────────────────

def why_route(c, mode):
    panels = [
        ("free",    "60-80% Cheaper",     "Route 70% of tasks to free",    "or near-free models."),
        ("budget",  "Quality Preserved",  "Premium models only when",      "the task truly needs it."),
        ("savings", "Quota Protected",    "Auto-downgrade near limits.",   "No more rate-limit walls."),
        ("primary", "Zero Config",        "Works out of the box with",     "Claude Pro/Max subscription."),
    ]
    icons = ["$", "★", "⚡", "⚙"]
    cards = ""
    for i, (color_key, title, line1, line2) in enumerate(panels):
        x = 18 + i * 218
        color = c[color_key]
        delay = i * 0.15
        cards += f'''
    <g opacity="0" style="animation: cardFadeIn 0.5s ease-out {delay}s both;">
      <rect x="{x}" y="10" width="200" height="130" rx="12" fill="{color}" opacity="0.08" stroke="{color}" stroke-width="1"/>
      <circle cx="{x+24}" cy="38" r="12" fill="{color}" opacity="0.2">
        <animate attributeName="r" values="11;14;11" dur="{2.5 + i*0.3}s" repeatCount="indefinite"/>
        <animate attributeName="opacity" values="0.2;0.35;0.2" dur="{2.5 + i*0.3}s" repeatCount="indefinite"/>
      </circle>
      <text x="{x+24}" y="43" text-anchor="middle" font-family="system-ui, sans-serif" font-size="14">{icons[i]}</text>
      <text x="{x+100}" y="75" text-anchor="middle" font-family="system-ui, sans-serif" font-size="15" font-weight="700" fill="{color}">{title}</text>
      <text x="{x+100}" y="98" text-anchor="middle" font-family="system-ui, sans-serif" font-size="11" fill="{c['text2']}">{line1}</text>
      <text x="{x+100}" y="114" text-anchor="middle" font-family="system-ui, sans-serif" font-size="11" fill="{c['text2']}">{line2}</text>
    </g>'''

    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 890 150" fill="none">
  <style>
    @keyframes cardFadeIn {{
      0% {{ opacity: 0; transform: translateY(10px); }}
      100% {{ opacity: 1; transform: translateY(0); }}
    }}
  </style>
  {cards}
</svg>'''


# ── 3. Savings Infographic ─────────────────────────────────────────────────

def savings_infographic(c, mode):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 890 200" fill="none">
  <style>
    @keyframes countUp {{
      0% {{ opacity: 0; transform: translateY(15px); }}
      100% {{ opacity: 1; transform: translateY(0); }}
    }}
    @keyframes barGrow {{
      0% {{ transform: scaleX(0); }}
      100% {{ transform: scaleX(1); }}
    }}
    @keyframes shimmer {{
      0% {{ opacity: 0.75; }}
      50% {{ opacity: 1; }}
      100% {{ opacity: 0.75; }}
    }}
    @keyframes numberPulse {{
      0%, 100% {{ transform: scale(1); }}
      50% {{ transform: scale(1.03); }}
    }}
    .big-stat {{ animation: countUp 0.8s ease-out both, numberPulse 4s ease-in-out 1s infinite; }}
    .bar-free {{ transform-origin: left center; animation: barGrow 0.8s ease-out 0.2s both; }}
    .bar-budget {{ transform-origin: left center; animation: barGrow 0.8s ease-out 0.5s both; }}
    .bar-premium {{ transform-origin: left center; animation: barGrow 0.8s ease-out 0.8s both; }}
    .bar-label {{ animation: countUp 0.5s ease-out 1.2s both; }}
    .shimmer {{ animation: shimmer 3s ease-in-out infinite; }}
  </style>

  <rect width="890" height="200" rx="14" fill="{c['bg2']}" stroke="{c['border']}" stroke-width="1"/>

  <!-- Left: big stat with pulse -->
  <text x="100" y="55" text-anchor="middle" font-family="system-ui, sans-serif" font-size="13" font-weight="600" fill="{c['text2']}" opacity="0.8">PROVEN SAVINGS</text>
  <g class="big-stat">
    <text x="100" y="115" text-anchor="middle" font-family="system-ui, sans-serif" font-size="58" font-weight="800" fill="{c['savings']}">87%</text>
  </g>
  <text x="100" y="140" text-anchor="middle" font-family="system-ui, sans-serif" font-size="13" fill="{c['text2']}">cost reduction</text>
  <text x="100" y="160" text-anchor="middle" font-family="system-ui, sans-serif" font-size="11" fill="{c['text2']}">$6.95 vs $50-60 baseline</text>
  <text x="100" y="178" text-anchor="middle" font-family="system-ui, sans-serif" font-size="11" fill="{c['text2']}">22.6M tokens &middot; 51 releases</text>

  <!-- Divider with gradient -->
  <line x1="210" y1="30" x2="210" y2="170" stroke="{c['border']}" stroke-width="1" opacity="0.5"/>

  <!-- Right: token distribution -->
  <text x="550" y="42" text-anchor="middle" font-family="system-ui, sans-serif" font-size="13" font-weight="600" fill="{c['text2']}" opacity="0.8">TOKEN DISTRIBUTION</text>

  <!-- Animated stacked bars -->
  <g class="bar-free">
    <rect x="240" y="60" width="186" height="36" rx="6" fill="{c['free']}" class="shimmer"/>
    <text x="333" y="83" text-anchor="middle" font-family="system-ui, sans-serif" font-size="13" font-weight="700" fill="white">31% Free</text>
  </g>
  <g class="bar-budget">
    <rect x="426" y="60" width="228" height="36" fill="{c['budget']}" class="shimmer"/>
    <text x="540" y="83" text-anchor="middle" font-family="system-ui, sans-serif" font-size="13" font-weight="700" fill="white">38% Budget</text>
  </g>
  <g class="bar-premium">
    <rect x="654" y="60" width="186" height="36" rx="6" fill="{c['premium']}" class="shimmer"/>
    <text x="747" y="83" text-anchor="middle" font-family="system-ui, sans-serif" font-size="13" font-weight="700" fill="white">31% Premium</text>
  </g>

  <!-- Labels under bar (fade in after bars) -->
  <g class="bar-label">
    <text x="333" y="116" text-anchor="middle" font-family="system-ui, sans-serif" font-size="11" fill="{c['free']}">7.0M tokens</text>
    <text x="333" y="132" text-anchor="middle" font-family="system-ui, sans-serif" font-size="11" font-weight="700" fill="{c['free']}">$0.00</text>
    <text x="333" y="148" text-anchor="middle" font-family="system-ui, sans-serif" font-size="10" fill="{c['text2']}">Ollama + Codex</text>
  </g>
  <g class="bar-label">
    <text x="540" y="116" text-anchor="middle" font-family="system-ui, sans-serif" font-size="11" fill="{c['budget']}">8.6M tokens</text>
    <text x="540" y="132" text-anchor="middle" font-family="system-ui, sans-serif" font-size="11" font-weight="700" fill="{c['budget']}">$2.82</text>
    <text x="540" y="148" text-anchor="middle" font-family="system-ui, sans-serif" font-size="10" fill="{c['text2']}">Flash + GPT-4o-mini</text>
  </g>
  <g class="bar-label">
    <text x="747" y="116" text-anchor="middle" font-family="system-ui, sans-serif" font-size="11" fill="{c['premium']}">7.0M tokens</text>
    <text x="747" y="132" text-anchor="middle" font-family="system-ui, sans-serif" font-size="11" font-weight="700" fill="{c['premium']}">$4.13</text>
    <text x="747" y="148" text-anchor="middle" font-family="system-ui, sans-serif" font-size="10" fill="{c['text2']}">GPT-4o + Claude</text>
  </g>

  <text x="550" y="180" text-anchor="middle" font-family="system-ui, sans-serif" font-size="12" fill="{c['text2']}">Annualized: ~$180/yr vs $1,500/yr baseline</text>
</svg>'''


# ── 4. Architecture / How It Works Diagram ─────────────────────────────────

def architecture(c, mode):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 890 280" fill="none">
  <style>
    @keyframes dataFlow {{
      0% {{ offset-distance: 0%; opacity: 0; }}
      10% {{ opacity: 1; }}
      90% {{ opacity: 1; }}
      100% {{ offset-distance: 100%; opacity: 0; }}
    }}
    @keyframes stageGlow {{
      0%, 100% {{ stroke-opacity: 0.5; }}
      50% {{ stroke-opacity: 1; }}
    }}
    @keyframes guardPulse {{
      0%, 100% {{ opacity: 0.06; }}
      50% {{ opacity: 0.14; }}
    }}
    @keyframes stageAppear {{
      0% {{ opacity: 0; transform: translateY(8px); }}
      100% {{ opacity: 1; transform: translateY(0); }}
    }}
    .stage-1 {{ animation: stageAppear 0.4s ease-out 0s both; }}
    .stage-2 {{ animation: stageAppear 0.4s ease-out 0.15s both; }}
    .stage-3 {{ animation: stageAppear 0.4s ease-out 0.3s both; }}
    .stage-4 {{ animation: stageAppear 0.4s ease-out 0.45s both; }}
    .stage-5 {{ animation: stageAppear 0.4s ease-out 0.6s both; }}
    .guard {{ animation: guardPulse 3s ease-in-out infinite; }}
    .guard-2 {{ animation: guardPulse 3s ease-in-out 1s infinite; }}
    .guard-3 {{ animation: guardPulse 3s ease-in-out 2s infinite; }}
    .pipe-glow {{ animation: stageGlow 2s ease-in-out infinite; }}
  </style>

  <defs>
    <marker id="a1" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto"><path d="M0 0 L10 5 L0 10z" fill="{c['primary']}"/></marker>
    <marker id="a2" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto"><path d="M0 0 L10 5 L0 10z" fill="{c['free']}"/></marker>
  </defs>

  <rect width="890" height="280" rx="14" fill="{c['bg2']}" stroke="{c['border']}" stroke-width="1"/>

  <!-- Title -->
  <text x="445" y="35" text-anchor="middle" font-family="system-ui, sans-serif" font-size="18" font-weight="700" fill="{c['text']}">How It Works</text>
  <text x="445" y="56" text-anchor="middle" font-family="system-ui, sans-serif" font-size="12" fill="{c['text2']}">Every prompt flows through a multi-layer classification and routing pipeline</text>

  <!-- Stage 1: Prompt -->
  <g class="stage-1">
    <rect x="20" y="100" width="110" height="80" rx="10" fill="{c['bg']}" stroke="{c['border']}"/>
    <text x="75" y="130" text-anchor="middle" font-family="system-ui, sans-serif" font-size="11" font-weight="600" fill="{c['text']}">User</text>
    <text x="75" y="148" text-anchor="middle" font-family="system-ui, sans-serif" font-size="11" font-weight="600" fill="{c['text']}">Prompt</text>
    <text x="75" y="168" text-anchor="middle" font-family="system-ui, sans-serif" font-size="9" fill="{c['text2']}">any task</text>
  </g>

  <!-- Animated connecting lines with flowing dots -->
  <line x1="130" y1="140" x2="170" y2="140" stroke="{c['primary']}" stroke-width="2" stroke-opacity="0.3"/>
  <circle r="3.5" fill="{c['primary']}"><animateMotion dur="1.5s" repeatCount="indefinite" path="M130,140 L170,140"/><animate attributeName="opacity" values="0;1;1;0" dur="1.5s" repeatCount="indefinite"/></circle>

  <!-- Stage 2: Heuristic -->
  <g class="stage-2">
    <rect class="pipe-glow" x="180" y="100" width="120" height="80" rx="10" fill="{c['primary']}" opacity="0.08" stroke="{c['primary']}" stroke-width="1.5"/>
    <text x="240" y="127" text-anchor="middle" font-family="system-ui, sans-serif" font-size="10" font-weight="700" fill="{c['primary']}">Heuristic</text>
    <text x="240" y="143" text-anchor="middle" font-family="system-ui, sans-serif" font-size="10" font-weight="700" fill="{c['primary']}">Fast-Path</text>
    <text x="240" y="162" text-anchor="middle" font-family="system-ui, sans-serif" font-size="9" fill="{c['text2']}">regex patterns</text>
    <text x="240" y="174" text-anchor="middle" font-family="system-ui, sans-serif" font-size="9" fill="{c['free']}">instant, free</text>
  </g>

  <line x1="300" y1="140" x2="340" y2="140" stroke="{c['primary']}" stroke-width="2" stroke-opacity="0.3"/>
  <circle r="3.5" fill="{c['primary']}"><animateMotion dur="1.5s" begin="0.4s" repeatCount="indefinite" path="M300,140 L340,140"/><animate attributeName="opacity" values="0;1;1;0" dur="1.5s" begin="0.4s" repeatCount="indefinite"/></circle>

  <!-- Stage 3: Classifier -->
  <g class="stage-3">
    <rect class="pipe-glow" x="350" y="100" width="120" height="80" rx="10" fill="{c['accent']}" opacity="0.08" stroke="{c['accent']}" stroke-width="1.5"/>
    <text x="410" y="127" text-anchor="middle" font-family="system-ui, sans-serif" font-size="10" font-weight="700" fill="{c['accent']}">Complexity</text>
    <text x="410" y="143" text-anchor="middle" font-family="system-ui, sans-serif" font-size="10" font-weight="700" fill="{c['accent']}">Classifier</text>
    <text x="410" y="162" text-anchor="middle" font-family="system-ui, sans-serif" font-size="9" fill="{c['text2']}">Ollama / Gemini Flash</text>
    <text x="410" y="174" text-anchor="middle" font-family="system-ui, sans-serif" font-size="9" fill="{c['free']}">free or ~$0.0001</text>
  </g>

  <line x1="470" y1="140" x2="510" y2="140" stroke="{c['primary']}" stroke-width="2" stroke-opacity="0.3"/>
  <circle r="3.5" fill="{c['accent']}"><animateMotion dur="1.5s" begin="0.8s" repeatCount="indefinite" path="M470,140 L510,140"/><animate attributeName="opacity" values="0;1;1;0" dur="1.5s" begin="0.8s" repeatCount="indefinite"/></circle>

  <!-- Stage 4: Router -->
  <g class="stage-4">
    <rect class="pipe-glow" x="520" y="85" width="140" height="110" rx="12" fill="{c['free']}" opacity="0.08" stroke="{c['free']}" stroke-width="1.5"/>
    <text x="590" y="110" text-anchor="middle" font-family="system-ui, sans-serif" font-size="11" font-weight="700" fill="{c['free']}">Free-First Router</text>
    <text x="590" y="132" text-anchor="middle" font-family="system-ui, sans-serif" font-size="9" fill="{c['text2']}">Ollama (free, local)</text>
    <text x="590" y="146" text-anchor="middle" font-family="system-ui, sans-serif" font-size="9" fill="{c['text2']}">Codex (prepaid)</text>
    <text x="590" y="160" text-anchor="middle" font-family="system-ui, sans-serif" font-size="9" fill="{c['text2']}">Gemini / GPT (budget)</text>
    <text x="590" y="174" text-anchor="middle" font-family="system-ui, sans-serif" font-size="9" fill="{c['text2']}">Claude / o3 (premium)</text>
    <text x="590" y="190" text-anchor="middle" font-family="system-ui, sans-serif" font-size="8" fill="{c['free']}">tries cheapest first</text>
  </g>

  <line x1="660" y1="140" x2="700" y2="140" stroke="{c['free']}" stroke-width="2" stroke-opacity="0.3"/>
  <circle r="3.5" fill="{c['free']}"><animateMotion dur="1.5s" begin="1.2s" repeatCount="indefinite" path="M660,140 L700,140"/><animate attributeName="opacity" values="0;1;1;0" dur="1.5s" begin="1.2s" repeatCount="indefinite"/></circle>

  <!-- Stage 5: Execute + Track -->
  <g class="stage-5">
    <rect class="pipe-glow" x="710" y="100" width="155" height="80" rx="10" fill="{c['savings']}" opacity="0.08" stroke="{c['savings']}" stroke-width="1.5"/>
    <text x="787" y="127" text-anchor="middle" font-family="system-ui, sans-serif" font-size="10" font-weight="700" fill="{c['savings']}">Execute + Track</text>
    <text x="787" y="148" text-anchor="middle" font-family="system-ui, sans-serif" font-size="9" fill="{c['text2']}">Run on best model</text>
    <text x="787" y="162" text-anchor="middle" font-family="system-ui, sans-serif" font-size="9" fill="{c['text2']}">Log decision + cost</text>
    <text x="787" y="176" text-anchor="middle" font-family="system-ui, sans-serif" font-size="9" fill="{c['savings']}">Update savings dashboard</text>
  </g>

  <!-- Guards with breathing pulse -->
  <rect class="guard" x="180" y="210" width="160" height="40" rx="8" fill="{c['budget']}" stroke="{c['budget']}" stroke-width="1" stroke-opacity="0.3"/>
  <text x="260" y="235" text-anchor="middle" font-family="system-ui, sans-serif" font-size="10" fill="{c['budget']}">Budget Pressure Guard</text>

  <rect class="guard-2" x="365" y="210" width="160" height="40" rx="8" fill="{c['premium']}" stroke="{c['premium']}" stroke-width="1" stroke-opacity="0.3"/>
  <text x="445" y="235" text-anchor="middle" font-family="system-ui, sans-serif" font-size="10" fill="{c['premium']}">Quality Health Check</text>

  <rect class="guard-3" x="550" y="210" width="160" height="40" rx="8" fill="{c['accent']}" stroke="{c['accent']}" stroke-width="1" stroke-opacity="0.3"/>
  <text x="630" y="235" text-anchor="middle" font-family="system-ui, sans-serif" font-size="10" fill="{c['accent']}">Circuit Breaker</text>

  <!-- Dashed connectors from guards -->
  <line x1="260" y1="180" x2="260" y2="210" stroke="{c['budget']}" stroke-width="1" stroke-dasharray="3,3" opacity="0.4"/>
  <line x1="445" y1="180" x2="445" y2="210" stroke="{c['premium']}" stroke-width="1" stroke-dasharray="3,3" opacity="0.4"/>
  <line x1="630" y1="195" x2="630" y2="210" stroke="{c['accent']}" stroke-width="1" stroke-dasharray="3,3" opacity="0.4"/>
</svg>'''


# ── 5. Editor Compatibility ────────────────────────────────────────────────

def editors(c, mode):
    editors_data = [
        ("Claude Code", "Full", c['free'], "Auto-routing"),
        ("Gemini CLI", "Full", c['free'], "Auto-routing"),
        ("Codex CLI", "Full", c['free'], "Auto-routing"),
        ("VS Code", "MCP", c['budget'], "48 tools"),
        ("Cursor", "MCP", c['budget'], "48 tools"),
        ("Any MCP", "MCP", c['budget'], "48 tools"),
    ]
    cards = ""
    for i, (name, level, color, desc) in enumerate(editors_data):
        x = 12 + i * 146
        delay = i * 0.1
        cards += f'''
    <g opacity="0" style="animation: editorIn 0.4s ease-out {delay}s both;">
      <rect x="{x}" y="10" width="132" height="90" rx="10" fill="{color}" opacity="0.06" stroke="{color}" stroke-width="1">
        <animate attributeName="stroke-opacity" values="0.4;0.9;0.4" dur="{3 + i*0.2}s" repeatCount="indefinite"/>
      </rect>
      <text x="{x+66}" y="38" text-anchor="middle" font-family="system-ui, sans-serif" font-size="13" font-weight="700" fill="{c['text']}">{name}</text>
      <rect x="{x+36}" y="48" width="60" height="20" rx="10" fill="{color}" opacity="0.2"/>
      <text x="{x+66}" y="62" text-anchor="middle" font-family="system-ui, sans-serif" font-size="10" font-weight="600" fill="{color}">{level}</text>
      <text x="{x+66}" y="88" text-anchor="middle" font-family="system-ui, sans-serif" font-size="10" fill="{c['text2']}">{desc}</text>
    </g>'''

    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 890 110" fill="none">
  <style>
    @keyframes editorIn {{
      0% {{ opacity: 0; transform: translateY(10px) scale(0.95); }}
      100% {{ opacity: 1; transform: translateY(0) scale(1); }}
    }}
  </style>
  {cards}
</svg>'''


# ── 6. Nav Buttons ─────────────────────────────────────────────────────────

def nav_button(c, mode, label, width=120):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} 36" fill="none">
  <style>
    @keyframes btnShine {{
      0%, 100% {{ stroke-opacity: 0.5; }}
      50% {{ stroke-opacity: 1; }}
    }}
    .btn-border {{ animation: btnShine 3s ease-in-out infinite; }}
  </style>
  <rect class="btn-border" width="{width}" height="36" rx="18" fill="{c['primary']}" opacity="0.1" stroke="{c['primary']}" stroke-width="1"/>
  <text x="{width//2}" y="23" text-anchor="middle" font-family="system-ui, sans-serif" font-size="13" font-weight="600" fill="{c['primary']}">{label}</text>
</svg>'''


# ── Generate all assets ───────────────────────────────────────────────────

if __name__ == "__main__":
    print("Generating animated README SVGs...")
    write_svg("hero", hero_banner)
    write_svg("why-route", why_route)
    write_svg("savings", savings_infographic)
    write_svg("architecture", architecture)
    write_svg("editors", editors)

    for label, w in [("Quick Start", 120), ("Docs", 80), ("Tool Reference", 130), ("Changelog", 110)]:
        slug = label.lower().replace(" ", "-")
        for mode_name, colors in [("light", LIGHT), ("dark", DARK)]:
            svg = nav_button(colors, mode_name, label, w)
            path = OUT / f"btn-{slug}-{mode_name}.svg"
            path.write_text(svg)
        print(f"  wrote btn-{slug}-*.svg")

    print(f"\nDone! {len(list(OUT.glob('*.svg')))} SVGs in {OUT}")
