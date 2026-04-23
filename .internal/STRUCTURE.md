# LLM Router — Internal Strategy Structure

**CONFIDENTIAL - INTERNAL USE ONLY**

This directory contains confidential product strategy, enterprise planning, and market hypotheses. 

**DO NOT**:
- Commit to public repositories
- Share with community members
- Publish in documentation
- Reference in GitHub issues or PRs
- Discuss in open channels

**DO**:
- Use for internal leadership alignment
- Use for enterprise partnership discussions
- Use for funding/investor conversations
- Treat as competitive advantage
- Update as strategy evolves

## Directory Organization

```
.internal/
├── STRUCTURE.md (this file)
├── strategy/
│   ├── product-strategy.md           # Product vision, market opportunity, enterprise thesis
│   ├── enterprise-thesis.md          # Why enterprises need this, willingness to pay
│   ├── packaging-strategy.md         # Open-core boundary, enterprise packaging
│   ├── moat-and-risks.md             # Defensibility, competitive risks
│   └── operating-model.md            # How we operate to maximize optionality
├── roadmap/
│   ├── versioned-execution-plan.md   # v3.5–v5.x in detail
│   ├── 90-day-plan.md                # Next 90 days: exact priorities, deliverables
│   ├── 180-day-plan.md               # Quarters 2–3: mid-term vision
│   └── 12-month-operating-plan.md    # Full-year strategic execution
├── architecture/
│   ├── control-plane-architecture.md # Detailed CP design, APIs, data model
│   ├── policy-model.md               # Policy abstraction, inheritance, scoping
│   ├── workflow-routing-design.md    # Multi-step task routing design
│   ├── token-optimization-engine.md  # Dedup, pruning, escalation designs
│   └── enterprise-security.md        # RBAC, SSO, audit, compliance
├── market/
│   ├── enterprise-hypotheses.md      # Customer hypotheses, pain points, willingness to pay
│   ├── pricing-hypotheses.md         # Pricing models, monetization strategy
│   ├── pilot-target-signals.md       # What signals indicate enterprise readiness
│   ├── competitive-analysis.md       # Competitors, differentiation, defensibility
│   └── sales-motion.md               # How to approach enterprise customers
├── decisions/
│   ├── adr-001-control-plane-scope.md
│   ├── adr-002-open-core-boundary.md
│   ├── adr-003-enterprise-packaging.md
│   ├── adr-004-stealth-strategy.md
│   └── (more as decisions are made)
└── experiments/
    ├── validation-experiments.md     # What to validate privately before committing
    └── market-signals.md             # What early traction signals look like
```

## Files to Create First

**Priority 1** (Week 1):
1. `strategy/product-strategy.md` — Align on vision
2. `strategy/enterprise-thesis.md` — Clarify commercial opportunity
3. `strategy/packaging-strategy.md` — Define boundaries early

**Priority 2** (Week 2):
4. `roadmap/90-day-plan.md` — Get moving
5. `roadmap/versioned-execution-plan.md` — Sequence work
6. `architecture/control-plane-architecture.md` — Start design

**Priority 3** (Week 3):
7. `market/enterprise-hypotheses.md` — Validate assumptions
8. `strategy/operating-model.md` — Clarify how we work

## .gitignore Rules

Add to root `.gitignore`:

```gitignore
# Internal strategy and confidential planning
.internal/
.strategy/
.private/
planning/hidden/

# Do not commit internal artifacts
!.internal/STRUCTURE.md  # This file documents the structure
```

This ensures:
- All strategy files are ignored
- Structure doc stays (for reference) but strategy files are local-only
- No accidental commits of sensitive material
- Easy to restore STRUCTURE.md for reference

## Confidentiality Model

**Open-Source Developers & Community**:
- See only public repo
- Can contribute to open-source features
- No visibility into commercial roadmap

**Internal Team / Leadership**:
- Full access to `.internal/` directory
- Understand commercial vision
- Aligned on sequencing and optionality

**Enterprise Partners (Under NDA)**:
- Limited view of selected architecture documents
- Product roadmap under NDA
- Pricing and packaging discussions confidential

**Potential Investors**:
- Executive summary of strategy
- Market opportunity and thesis
- Selective architecture and product details

## Version Control Discipline

**Public commits** (visible in git history):
- Open-source features only
- Bug fixes
- Infrastructure improvements
- Documentation

**Private work** (not in git):
- Strategic planning documents
- Enterprise design docs
- Market hypotheses and validation results
- Pricing models
- Internal architecture decisions

**Hybrid approach** (commits + private notes):
- Merge features to main with no strategic context
- Document detailed reasoning in `.internal/decisions/adr-*`
- Reference ADR in commit message (without exposing strategy)

## This Structure Enables

✅ **Stealth execution** — Work on enterprise features without revealing direction  
✅ **Optionality** — Keep all options open (company, enterprise product, managed service)  
✅ **Competitive advantage** — No preview of roadmap to competitors  
✅ **Enterprise credibility** — Can show detailed enterprise roadmap under NDA  
✅ **Investor readiness** — Documentation ready if/when fundraising starts  
✅ **Team alignment** — Clear shared understanding of strategic direction  

---

Next: Read `strategy/product-strategy.md`
