# SpecPilot

AI review assistant for the ITSG system review of payroll software (Futury Hackathon 2026).
Scope: income replacement benefits (Entgeltersatzleistungen), demo case reason code 41 / DBVO.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then put the API key into .env
```

Copy the ITSG documents (not committed) into:
- `data/specs/` – the PDFs (Verfahrensbeschreibung, Beispielpapier, ...)
- `data/samples/` – `TEEL0-Test1-DBVO` and other output files

## Layout

```
specpilot/
  config.py     settings (paths, key, models)
  agents/       Administrative, Extractor, Testing, Validation
  tools/        code the agents use (PDF ingest, quote check, parser, ...)
data/           ITSG documents (local only)
tests/
```
