# BossHunter v2.0

> A local-first AI job-search assistant for job discovery, two-stage scoring, human review, personalized greetings, resume generation, controlled sending, and reply monitoring.

[中文说明](README.md) · [Documentation](docs/README.md) · [Contributing](CONTRIBUTING.md) · [License](LICENSE)

## Overview

BossHunter combines AI analysis with a local browser runtime connected through the Chrome DevTools Protocol (CDP). It helps an individual job seeker collect and evaluate openings, prepare tailored messages and resumes, and track application activity.

BossHunter is not a fully autonomous mass-application tool. Applications and other sensitive actions must remain subject to human review. Resume-request detection and suggested replies classify or generate content; they do not automatically click consent, refusal, attachment, or send controls.

## Features

- Keyword and city-based job collection with deduplication
- Keyword pre-filtering followed by in-depth AI scoring
- Personalized greeting generation from the job description and your resume
- Mandatory human review before applications are sent
- Randomized intervals, sending windows, daily limits, and progressive backoff
- HR reply monitoring and follow-up tracking
- Tailored PDF resumes with modern, executive, and ATS templates
- A local React dashboard for configuration, review, status, and artifacts
- Anthropic Messages and OpenAI-compatible AI providers

## Requirements

- Python 3.11+
- Node.js 22+
- PostgreSQL 14+ (SQLite fallback is not supported)
- A current Chrome or Edge installation
- An Anthropic or OpenAI-compatible API credential

## Quick Start

```bash
git clone https://github.com/powerycy/BossHunter.git
cd BossHunter
python -m venv .venv

# Windows Git Bash
source .venv/Scripts/activate

# macOS / Linux
# source .venv/bin/activate

python -m pip install -e .
cp config.example.yaml config.yaml
cp resume.example.md resume.md
```

Create a PostgreSQL database and apply every SQL file in `migrations/postgresql/` in numeric order. Then configure at least:

- `database.url`
- your own `profile.resume_path`
- search roles, cities, and keywords
- an AI provider, model, and API key
- conservative sending limits and windows

On Windows, start the isolated browser and local dashboard with:

```bash
bosshunter start
```

Sign in to the recruitment platform manually in the browser that opens. For an existing browser with remote debugging enabled:

```bash
bosshunter connect
bosshunter web
```

The dashboard is available at `http://127.0.0.1:8686` by default. Start with a small, step-by-step run and review every candidate before sending.

## Common Commands

```bash
bosshunter scrape -k "Python developer" -l 10
bosshunter reenrich -l 10
bosshunter score
bosshunter greet
bosshunter confirm
bosshunter send
bosshunter monitor --once
bosshunter status --full
```

`bosshunter run` executes the end-to-end pipeline, but it does not remove mandatory human confirmation.

## Data and Security

- Recruitment-platform credentials are not stored by BossHunter. The runtime connects to a browser session you log in to yourself.
- Keep `config.yaml`, `.env`, resumes, generated artifacts, databases, and `data/browser-profile/` private.
- Prefer environment variables for AI credentials.
- The dashboard binds to `127.0.0.1` by default. Binding to a non-loopback address requires `BOSSHUNTER_AUTH_TOKEN` for API access.
- Do not upload sensitive logs or real resumes to public issues.

## Responsible Use

Browser automation may violate a third-party platform's terms and may result in account restrictions. Use only your own account and resume, keep the rate low, stop on CAPTCHAs or risk warnings, and comply with applicable terms and laws. This project does not provide a guarantee against detection or account action.

BossHunter is not affiliated with, endorsed by, or connected to any recruitment platform.

## Documentation

The detailed documentation is currently maintained in Chinese:

- [Documentation index](docs/README.md)
- [Installation and upgrade guide](docs/安装与升级指南.md)
- [Configuration reference](docs/配置参考.md)
- [User guide](docs/操作手册.md)
- [Troubleshooting](docs/故障排查.md)
- [Technical manual](docs/技术手册.md)

## Contributing

Issues and pull requests are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) first. Changes that bypass platform safeguards, weaken mandatory review, increase default sending frequency, or exfiltrate user data will not be accepted.

## License

This repository uses the [BossHunter Non-Commercial License](LICENSE), identified as `LicenseRef-BossHunter-NonCommercial` in `pyproject.toml`. Personal, educational, research, and other non-commercial use is permitted under the license terms. Commercial use requires prior written permission from the copyright holder.

## Disclaimer

The software is provided "as is," without warranty. You are solely responsible for how you use it and for any account restriction, data loss, legal dispute, or other consequence arising from that use.
