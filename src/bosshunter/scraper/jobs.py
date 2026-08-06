"""Job scraping module - Extract jobs from BOSS直聘 search results."""

import json
import random
import re
import time
import hashlib
from urllib.parse import quote

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from bosshunter.browser import (
    new_tab, close_tab, evaluate, scroll, wait_for_load
)
from bosshunter.config import CITY_CODES
from bosshunter.db import get_db, get_job, get_jobs_needing_enrichment, insert_job, update_job_enrichment
from bosshunter.job_filters import is_agency_company, matching_deal_breaker
from bosshunter.throttle import PageThrottle

console = Console()

# BOSS直聘搜索页 URL 模板
SEARCH_URL = "https://www.zhipin.com/web/geek/job?query={keyword}&city={city_code}"


def _stop_requested(config: dict) -> bool:
    """Return whether a dashboard task has requested cooperative cancellation."""
    stop_event = config.get("_workbench_stop_event")
    return bool(stop_event and stop_event.is_set())


def _wait_or_stop(config: dict, seconds: float) -> bool:
    """Wait interruptibly and report whether cancellation was requested."""
    stop_event = config.get("_workbench_stop_event")
    if stop_event:
        return bool(stop_event.wait(seconds))
    time.sleep(seconds)
    return False

# JS: 从搜索列表页提取岗位卡片数据
JS_EXTRACT_LIST = """
(() => {
    const wraps = document.querySelectorAll('.job-card-wrap');
    const jobs = [];
    wraps.forEach((wrap) => {
        const box = wrap.querySelector('.job-card-box') || wrap;
        const nameEl = box.querySelector('.job-name');
        const salaryEl = box.querySelector('.job-salary');
        const tags = box.querySelectorAll('.tag-list li');
        const companyEl = box.querySelector('.boss-name') || box.querySelector('.company-name');
        const locationEl = box.querySelector('.company-location');
        const href = nameEl ? nameEl.getAttribute('href') : '';

        if (!nameEl || !href) return;

        jobs.push({
            title: nameEl.textContent.trim(),
            salary: salaryEl ? salaryEl.textContent.trim() : '',
            experience: tags[0] ? tags[0].textContent.trim() : '',
            education: tags[1] ? tags[1].textContent.trim() : '',
            company: companyEl ? companyEl.textContent.trim() : '',
            location: locationEl ? locationEl.textContent.trim() : '',
            url: href
        });
    });
    return JSON.stringify(jobs);
})()
"""

# JS: 从详情页提取完整岗位信息
JS_EXTRACT_DETAIL = """
(() => {
    const info = {};
    // Title and salary
    info.title = document.querySelector('.info-primary .name h1')?.textContent?.trim()
        || document.querySelector('.name h1')?.textContent?.trim()
        || document.title.split('-')[0]?.trim();
    info.salary = document.querySelector('.info-primary .salary')?.textContent?.trim()
        || document.querySelector('.salary')?.textContent?.trim() || '';

    // Tags (experience, education, etc)
    const tagItems = document.querySelectorAll('.info-primary .tag-list span');
    const tagTexts = Array.from(tagItems).map(t => t.textContent.trim());
    info.experience = tagTexts[0] || '';
    info.education = tagTexts[1] || '';
    const bodyText = document.body?.innerText || '';
    const locationCandidates = document.querySelectorAll(
        '.info-primary .job-location, .job-location, .job-area, [class*="job-location"], [class*="job-area"]'
    );
    info.location = Array.from(locationCandidates)
        .map(el => (el.textContent || '').trim())
        .find(text => text) || '';
    const addressMatch = bodyText.match(/工作地址\\s*\\n([^\\n]+)/);
    if (addressMatch && /区|县/.test(addressMatch[1])) {
        info.location = addressMatch[1].trim();
    } else if (!info.location) {
        info.location = addressMatch ? addressMatch[1].trim() : '';
    }

    // JD
    info.jd = document.querySelector('.job-sec-text')?.textContent?.trim() || '';

    // Company info - try multiple selectors
    const companyLinks = document.querySelectorAll('.sider-company .company-info a');
    info.company = '';
    for (const link of companyLinks) {
        const text = link.textContent.trim();
        if (text && text.length > 0 && !text.includes('http')) {
            info.company = text;
            break;
        }
    }
    if (!info.company) {
        // Fallback: extract from page title "「职位」_公司名招聘"
        const titleMatch = document.title.match(/_(.+?)招聘/);
        info.company = titleMatch ? titleMatch[1] : '';
    }

    // Company details
    const companyTags = document.querySelectorAll(
        '.sider-company .res-industry-item, .sider-company .company-tag, .sider-company .tag, '
        + '.job-detail-company .company-tag, .job-detail-company .tag, .company-info-item, '
        + '[class*="company-info"] .tag, [class*="company-info"] li'
    );
    info.company_size = '';
    info.company_industry = '';
    const companyFacts = [];
    companyTags.forEach(tag => {
        const text = (tag.textContent || '').trim();
        if (text && text.length <= 80 && !companyFacts.includes(text)) companyFacts.push(text);
        if (!info.company_size && /(?:\\d+\\s*[-~至]\\s*\\d+|\\d+\\+?)\\s*人/.test(text)) info.company_size = text;
        else if (!info.company_industry) info.company_industry = text;
    });
    if (!companyFacts.length) {
        const companyBlocks = document.querySelectorAll('.sider-company, .job-detail-company, [class*="company-info"]');
        companyBlocks.forEach(block => {
            const lines = (block.innerText || '').split('\\n').map(line => line.trim()).filter(Boolean);
            lines.forEach(text => {
                const noise = /^(公司基本信息|公司介绍|工商信息|公司名称|法定代表人|成立日期|企业类型|经营状态|注册资金|查看全部职位|查看全部|工作地址|点击查看地图|职位描述)$/;
                const isNoise = noise.test(text) || /元\\/天|招聘|\\d+-\\d+人/.test(text);
                if (text.length <= 80 && !isNoise && !companyFacts.includes(text)) companyFacts.push(text);
                if (!info.company_size && /(?:\\d+\\s*[-~至]\\s*\\d+|\\d+\\+?)\\s*人/.test(text)) info.company_size = text;
            });
        });
    }
    info.company_background = companyFacts.join(' · ');
    const companyText = Array.from(document.querySelectorAll(
        '.sider-company, .job-detail-company, [class*="company-info"]'
    )).map(el => el.innerText || '').join('\\n') || bodyText;
    const bodySizeMatch = bodyText.match(/(?:\\d+\\s*[-~至]\\s*\\d+|\\d+\\+?)\\s*人/);
    if (!info.company_size && bodySizeMatch) info.company_size = bodySizeMatch[0];
    if (!info.company_background) {
        const backgroundLines = companyText.split('\\n')
            .map(line => line.trim())
            .filter(line => line && line.length <= 80 && !/^公司(名称|介绍|基本信息)$/.test(line));
        info.company_background = Array.from(new Set(backgroundLines)).slice(0, 8).join(' · ');
    }
    const capitalMatch = companyText.match(/(?:注册资本|注册资金)[：:\\s]*([^\\n]{1,40})/);
    info.registered_capital = capitalMatch ? capitalMatch[1].trim() : '';
    if (!info.registered_capital) {
        const bodyCapitalMatch = bodyText.match(/(?:注册资本|注册资金)\\s*\\n([^\\n]+)/);
        info.registered_capital = bodyCapitalMatch ? bodyCapitalMatch[1].trim() : '';
    }

    // HR info
    const bossSection = document.querySelector('.boss-info-attr') || document.querySelector('.job-boss-info');
    if (bossSection) {
        const nameEl = bossSection.querySelector('.name');
        const titleEl = bossSection.querySelector('.title');
        info.hr_name = nameEl?.textContent?.trim() || '';
        info.hr_title = titleEl?.textContent?.trim() || '';
    } else {
        info.hr_name = '';
        info.hr_title = '';
    }
    info.hr_active = document.querySelector('.boss-active-time')?.textContent?.trim() || '';

    // URL
    info.url = window.location.pathname;

    return JSON.stringify(info);
})()
"""


def _generate_job_id(url: str) -> str:
    """Generate a unique job ID from URL path.

    Supports BOSS直聘 detail-page variants:
    - ``/job_detail/{id}.html`` (desktop standard)
    - ``/share/job/{id}`` (mobile/share short link)
    - ``/job_detail/{id}`` (extensionless)
    Falls back to an md5-derived id when the URL matches none of them, so the
    job is still importable even if the platform changes path shapes.
    """
    match = re.search(r'/job_detail/([^./?#]+)', url)
    if match:
        return match.group(1)
    match = re.search(r'/share/job/([^/?#]+)', url)
    if match:
        return match.group(1)
    return hashlib.md5(url.encode()).hexdigest()[:16]


def _extract_district(location: str, city: str) -> str:
    """Extract the district-level location displayed in a BOSS job location string."""
    normalized = re.sub(r"\\s+", "", location or "")
    if not normalized:
        return ""

    for token in re.split(r"[·-]", normalized):
        if token.endswith(("区", "县", "市")) and token != city:
            return token

    match = re.search(r"([^·-]{1,12}(?:区|县))", normalized)
    return match.group(1) if match else ""


def scrape_jobs(config: dict, keywords: list[str], limit: int | None = None) -> int:
    """Scrape jobs from BOSS直聘 and store in database.

    Supports multi-keyword × multi-city combinations with pagination.
    When limit is None, collection is bounded only by city × keyword × max_pages.
    Returns the number of new jobs added.
    """
    db = get_db()
    throttle = PageThrottle(delay_min=2.0, delay_max=5.0)
    deal_breakers = config.get("profile", {}).get("deal_breakers", [])
    new_count = 0

    # Pagination config
    search_config = config.get("search", {})
    max_pages = min(search_config.get("max_pages", 3), 10)  # Hard cap: 10 pages

    # Resolve cities: search.cities > profile.target_cities > ["北京"]
    cities = search_config.get("cities", [])
    if not cities:
        cities = config.get("profile", {}).get("target_cities", ["北京"])

    # Build search combinations: city × keyword
    search_combos = []
    for city in cities:
        city_code = CITY_CODES.get(city)
        if not city_code:
            console.print(f"[yellow]⚠ 未识别的城市: {city}，已跳过[/yellow]")
            continue
        for keyword in keywords:
            search_combos.append((city, city_code, keyword))

    if not search_combos:
        console.print("[red]没有有效的搜索组合（检查城市配置）[/red]")
        db.close()
        return 0

    console.print(f"[dim]搜索组合: {len(search_combos)} 个 ({len(cities)}城市 × {len(keywords)}关键词 × {max_pages}页)[/dim]")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console
    ) as progress:
        for city, city_code, keyword in search_combos:
            if _stop_requested(config):
                break
            if limit is not None and new_count >= limit:
                break

            label = f"{city}/{keyword}" if len(cities) > 1 else keyword
            task = progress.add_task(f"搜索: {label}", total=None)
            keyword_new = 0

            for page in range(1, max_pages + 1):
                if _stop_requested(config):
                    break
                if limit is not None and new_count >= limit:
                    break

                # Build paginated URL
                search_url = SEARCH_URL.format(keyword=quote(keyword), city_code=city_code)
                sort_mode = search_config.get("sort", "")
                if sort_mode == "newest":
                    search_url += "&sortType=2"
                if page > 1:
                    search_url += f"&page={page}"

                # Open search page
                target_id = new_tab(search_url)
                if not target_id:
                    if page == 1:
                        progress.update(task, description=f"[red]✗ 无法打开搜索页: {label}[/red]")
                    break

                if _wait_or_stop(config, 3):
                    close_tab(target_id)
                    break
                wait_for_load(target_id, timeout=10)

                if _stop_requested(config):
                    close_tab(target_id)
                    break

                # Scroll to load all results on this page
                scroll(target_id, y=2000)
                if _wait_or_stop(config, 1.5):
                    close_tab(target_id)
                    break
                scroll(target_id, y=4000)
                if _wait_or_stop(config, 1.5):
                    close_tab(target_id)
                    break

                # Extract job list
                result = evaluate(target_id, JS_EXTRACT_LIST)
                if not result:
                    close_tab(target_id)
                    break

                try:
                    jobs_list = json.loads(result)
                except (json.JSONDecodeError, TypeError):
                    close_tab(target_id)
                    break

                close_tab(target_id)

                # No results on this page, stop pagination
                if not jobs_list:
                    break

                progress.update(task, description=f"搜索: {label} 第{page}页 ({len(jobs_list)}条)")

                # Process each job
                for job_data in jobs_list:
                    if _stop_requested(config):
                        break
                    if limit is not None and new_count >= limit:
                        break

                    job_url = job_data.get("url", "")
                    job_id = _generate_job_id(job_url)

                    existing_job = get_job(db, job_id)
                    if existing_job and all(existing_job.get(field) for field in (
                        "district", "company_background", "registered_capital"
                    )):
                        continue

                    # Skip deal breakers
                    if matching_deal_breaker(job_data.get("title", ""), deal_breakers):
                        continue

                    # Skip agency/headhunter jobs with vague company names (e.g. "某大型XX公司")
                    if is_agency_company(job_data.get("company", "")):
                        continue

                    # Open detail page for full JD
                    throttle.wait()
                    if _stop_requested(config):
                        break
                    detail_url = f"https://www.zhipin.com{job_url}"
                    detail_target = new_tab(detail_url)
                    if not detail_target:
                        continue

                    if _wait_or_stop(config, 2):
                        close_tab(detail_target)
                        break
                    wait_for_load(detail_target, timeout=10)

                    if _stop_requested(config):
                        close_tab(detail_target)
                        break

                    # Extract detail
                    detail_result = evaluate(detail_target, JS_EXTRACT_DETAIL)
                    close_tab(detail_target)

                    if not detail_result:
                        continue

                    try:
                        detail = json.loads(detail_result)
                    except (json.JSONDecodeError, TypeError):
                        continue

                    # Build job record
                    job_record = {
                        "id": job_id,
                        "title": detail.get("title", job_data.get("title", "")),
                        "company": detail.get("company", job_data.get("company", "")),
                        "salary": detail.get("salary", job_data.get("salary", "")),
                        "city": city,
                        "district": _extract_district(
                            detail.get("location", "") or job_data.get("location", ""), city
                        ),
                        "experience": detail.get("experience", job_data.get("experience", "")),
                        "jd": detail.get("jd", ""),
                        "hr_name": detail.get("hr_name", ""),
                        "hr_title": detail.get("hr_title", ""),
                        "hr_active": detail.get("hr_active", ""),
                        "company_size": detail.get("company_size", ""),
                        "company_industry": detail.get("company_industry", ""),
                        "company_background": detail.get("company_background", ""),
                        "registered_capital": detail.get("registered_capital", ""),
                        "url": detail_url,
                    }

                    if existing_job:
                        update_job_enrichment(db, job_record)
                        progress.update(task, description=f"搜索: {label} 第{page}页 (补充企业信息)")
                    else:
                        insert_job(db, job_record)
                        new_count += 1
                        keyword_new += 1
                        progress.update(task, description=f"搜索: {label} 第{page}页 (新增 {keyword_new})")

                # Anti-scraping: pause between pages
                if page < max_pages:
                    if _wait_or_stop(config, random.uniform(3.0, 6.0)):
                        break

            progress.update(task, description=f"搜索: {label} (新增 {keyword_new})")

    db.close()
    return new_count


def reenrich_jobs(
	config: dict,
	limit: int | None = None,
	statuses: list[str] | None = None,
) -> int:
	"""Re-open detail pages to fill missing company/location metadata.

	Unlike ``scrape_jobs`` (which searches by keyword+city), this re-visits
	jobs already in the DB that are missing district / company_background /
	registered_capital. It only fills blanks via ``update_job_enrichment`` —
	score, status, greeting, and JD are never touched.

	Args:
		config: App config (unused for search, kept for API symmetry).
		limit: Max number of jobs to re-enrich in this run (for batching /
			rate-control). None = no limit.
		statuses: Only re-enrich jobs in these statuses. None = all statuses.
			e.g. ["ready", "pending"] to skip already-sent jobs.

	Returns the number of jobs whose metadata was updated.
	"""
	db = get_db()
	jobs = get_jobs_needing_enrichment(db, statuses)
	if limit is not None:
		jobs = jobs[:limit]

	if not jobs:
		console.print("[yellow]没有需要补全企业信息的岗位[/yellow]")
		db.close()
		return 0

	throttle = PageThrottle(delay_min=2.0, delay_max=5.0)
	updated = 0
	failed = 0
	missing_on_page = 0

	console.print(f"[bold]开始补全 {len(jobs)} 个岗位的企业信息[/bold]（仅填空字段，不影响评分/状态）")

	with Progress(
		SpinnerColumn(),
		TextColumn("[progress.description]{task.description}"),
		console=console,
	) as progress:
		task = progress.add_task("补全中", total=len(jobs))

		for job in jobs:
			if _stop_requested(config):
				break
			progress.update(
				task,
				description=f"补全: {job['company'][:12]} - {job['title'][:12]}",
			)

			# 岗位可能已下架或 URL 失效，逐个容错。
			if not job.get("url"):
				failed += 1
				progress.update(task, advance=1)
				continue

			throttle.wait()
			if _stop_requested(config):
				break
			detail_target = new_tab(job["url"])
			if not detail_target:
				failed += 1
				progress.update(task, advance=1)
				continue

			if _wait_or_stop(config, 2):
				close_tab(detail_target)
				break
			wait_for_load(detail_target, timeout=10)
			if _stop_requested(config):
				close_tab(detail_target)
				break
			detail_result = evaluate(detail_target, JS_EXTRACT_DETAIL)
			close_tab(detail_target)

			if not detail_result:
				failed += 1
				progress.update(task, advance=1)
				continue

			try:
				detail = json.loads(detail_result)
			except (json.JSONDecodeError, TypeError):
				failed += 1
				progress.update(task, advance=1)
				continue

			# 复用采集期的字段映射 + 区域解析，保证与首次采集一致。
			job_record = {
				"id": job["id"],
				"district": _extract_district(
					detail.get("location", "") or "", job.get("city", "")
				),
				"company_size": detail.get("company_size", ""),
				"company_industry": detail.get("company_industry", ""),
				"company_background": detail.get("company_background", ""),
				"registered_capital": detail.get("registered_capital", ""),
			}
			update_job_enrichment(db, job_record)
			new_values = {
				field: str(job_record.get(field) or "").strip()
				for field in ("district", "company_size", "company_background", "registered_capital")
			}
			changed = any(
				not str(job.get(field) or "").strip() and value
				for field, value in new_values.items()
			)
			if changed:
				updated += 1
			else:
				missing_on_page += 1
			progress.update(task, advance=1)

	if failed > 0:
		console.print(f"[yellow]  {failed} 个岗位详情页打开失败（可能已下架），已跳过[/yellow]")
	if missing_on_page > 0:
		console.print(f"[yellow]  {missing_on_page} 个岗位详情页未提供可补全的企业信息[/yellow]")
	console.print(f"[green]✓[/green] 补全完成，{updated} 个岗位的企业信息已更新")

	db.close()
	return updated


def _normalize_detail_url(url: str) -> str:
	"""Resolve a user-provided job URL to a full desktop detail-page URL.

	Accepts absolute URLs, protocol-relative URLs, bare paths
	(``/job_detail/xxx.html``), and ``/share/job/xxx`` short links.
	Share links resolve to the canonical ``/job_detail/{id}.html`` so the same
	detail-page extractor applies uniformly.
	"""
	url = (url or "").strip()
	if not url:
		return ""
	# /share/job/{id} 短链 → 标准详情页
	share_match = re.search(r"/share/job/([^/?#]+)", url)
	if share_match and "/job_detail/" not in url:
		return f"https://www.zhipin.com/job_detail/{share_match.group(1)}.html"
	# 补全协议/主机
	if url.startswith("//"):
		return "https:" + url
	if url.startswith("/"):
		return "https://www.zhipin.com" + url
	if not re.match(r"^https?://", url):
		url = "https://" + url
	return url


def import_job_by_url(url: str, config: dict) -> dict | None:
	"""Collect a single job by its detail-page URL. Return a job record (not persisted).

	Unlike ``scrape_jobs`` (keyword × city batch loop with deal-breaker / agency
	filtering and throttling), this is a targeted one-shot collector: the caller
	has already decided this job is interesting, so we skip all automated
	filtering and only open the detail page once. Returns ``None`` if the page
	could not be opened or parsed.

	Args:
		url: Job detail URL or path (see ``_normalize_detail_url``).
		config: App config. Used for the workbench stop event so the import can
			be cancelled cooperatively from the dashboard.

	Returns:
		A job record dict with keys matching ``insert_job`` / ``update_job_enrichment``,
		or ``None`` on failure. The record is **not** written to the DB here —
		the caller decides whether to insert or enrich.
	"""
	detail_url = _normalize_detail_url(url)
	if not detail_url:
		return None

	job_id = _generate_job_id(detail_url)
	if not job_id:
		return None

	if _stop_requested(config):
		return None

	console.print(f"[bold]定向采集:[/bold] {detail_url}")
	target_id = new_tab(detail_url)
	if not target_id:
		console.print("[red]✗ 无法打开详情页（检查 BOSS 登录状态 / CDP 运行时）[/red]")
		return None

	try:
		if _wait_or_stop(config, 2):
			return None
		wait_for_load(target_id, timeout=10)
		if _stop_requested(config):
			return None
		# 详情页较长，滚动一次让懒加载的 HR/公司信息渲染完整。
		scroll(target_id, y=800)
		if _wait_or_stop(config, 1):
			return None
		detail_result = evaluate(target_id, JS_EXTRACT_DETAIL)
	finally:
		close_tab(target_id)

	if not detail_result:
		console.print("[red]✗ 详情页解析为空[/red]")
		return None

	try:
		detail = json.loads(detail_result)
	except (json.JSONDecodeError, TypeError):
		console.print("[red]✗ 详情页解析失败[/red]")
		return None

	# 复用批量采集期的字段映射逻辑（见 scrape_jobs 内 job_record 构造），
	# 保证导入岗位的字段结构与批量采集完全一致。
	city = _infer_city_from_location(detail.get("location", "")) or config.get(
		"profile", {}
	).get("target_cities", [""])[0] or ""
	job_record = {
		"id": job_id,
		"title": detail.get("title", ""),
		"company": detail.get("company", ""),
		"salary": detail.get("salary", ""),
		"city": city,
		"district": _extract_district(detail.get("location", ""), city),
		"experience": detail.get("experience", ""),
		"jd": detail.get("jd", ""),
		"hr_name": detail.get("hr_name", ""),
		"hr_title": detail.get("hr_title", ""),
		"hr_active": detail.get("hr_active", ""),
		"company_size": detail.get("company_size", ""),
		"company_industry": detail.get("company_industry", ""),
		"company_background": detail.get("company_background", ""),
		"registered_capital": detail.get("registered_capital", ""),
		"url": detail_url,
	}
	if not job_record["title"] or not job_record["company"]:
		console.print("[red]✗ 详情页缺少岗位名或公司名，疑似未登录或页面改版[/red]")
		return None
	console.print(
		f"[green]✓[/green] 已采集: {job_record['company']} - {job_record['title']}"
	)
	return job_record


def _infer_city_from_location(location: str) -> str:
	"""Best-effort guess of the city name from a BOSS location string.

	The detail page only carries a free-form location like ``北京·朝阳区·望京``;
	we need a city name to populate ``jobs.city`` and feed ``_extract_district``.
	Returns "" if nothing looks like a Chinese city.
	"""
	normalized = re.sub(r"\s+", "", location or "")
	if not normalized:
		return ""
	# 第一个 ·/- 之前的 token 通常是城市（"北京·朝阳区" → "北京"）
	first_token = re.split(r"[·\-·]", normalized)[0]
	if first_token and any(ch in first_token for ch in ("市", "省")) or re.fullmatch(
		r"[\u4e00-\u9fa5]{2,6}", first_token or ""
	):
		return first_token
	return first_token or ""


def search_jobs_by_company_title(
	company: str,
	title: str,
	config: dict,
	max_results: int = 10,
) -> list[dict]:
	"""Search candidate jobs by company + title. Return list cards (no detail fetch).

	Used by the dashboard's "按公司+职位" import tab: we want a short candidate
	list the user can pick from, so we only scrape the search-list page (cheap,
	one page load) and skip the per-card detail fetch + throttling that
	``scrape_jobs`` performs. Filtering by company is done **client-side** on
	the returned cards because the BOSS search ``query`` only matches job title;
	the company filter is what makes this "targeted".

	Args:
		company: Company name substring to filter on (after search).
		title: Job-title keyword used as the BOSS search ``query``.
		config: App config (read for the default city + stop event).
		max_results: Cap on returned candidates.

	Returns:
		List of ``{id, title, company, salary, location, url, experience}`` dicts.
		The ``url`` is the absolute detail-page URL ready for ``import_job_by_url``.
	"""
	company = (company or "").strip()
	title = (title or "").strip()
	if not title:
		# 公司名兜底当关键词，避免空 query 打不开搜索页。
		title = company
	if not title:
		return []

	# 用配置里的第一个城市；没有就退到"全国"（100010000）。
	cities = config.get("search", {}).get("cities") or config.get("profile", {}).get(
		"target_cities", []
	)
	city_name = cities[0] if cities else ""
	city_code = CITY_CODES.get(city_name) or "100010000"  # 100010000 = 全国

	search_url = SEARCH_URL.format(keyword=quote(title), city_code=city_code)
	console.print(f"[bold]定向搜索:[/bold] title={title!r} city={city_name or '全国'}")

	target_id = new_tab(search_url)
	if not target_id:
		console.print("[red]✗ 无法打开搜索页[/red]")
		return []

	try:
		if _wait_or_stop(config, 3):
			return []
		wait_for_load(target_id, timeout=10)
		if _stop_requested(config):
			return []
		# 滚动加载更多卡片，提高命中候选的概率。
		scroll(target_id, y=2000)
		if _wait_or_stop(config, 1.5):
			return []
		scroll(target_id, y=4000)
		if _wait_or_stop(config, 1.0):
			return []
		result = evaluate(target_id, JS_EXTRACT_LIST)
	finally:
		close_tab(target_id)

	if not result:
		return []
	try:
		cards = json.loads(result)
	except (json.JSONDecodeError, TypeError):
		return []

	# 客户端按公司名过滤（BOSS query 只匹配职位名，公司过滤要自己做）。
	company_norm = company.replace(" ", "").lower() if company else ""
	candidates: list[dict] = []
	for card in cards:
		card_company = (card.get("company") or "").strip()
		if company_norm:
			if company_norm not in card_company.replace(" ", "").lower():
				continue
		href = card.get("url", "")
		if not href:
			continue
		# href 通常是相对路径 "/job_detail/xxx.html"，列表卡里直接带绝对化。
		absolute_url = href if href.startswith("http") else f"https://www.zhipin.com{href}"
		candidates.append(
			{
				"id": _generate_job_id(absolute_url),
				"title": (card.get("title") or "").strip(),
				"company": card_company,
				"salary": (card.get("salary") or "").strip(),
				"experience": (card.get("experience") or "").strip(),
				"location": (card.get("location") or "").strip(),
				"url": absolute_url,
			}
		)
		if len(candidates) >= max_results:
			break

	console.print(f"[green]✓[/green] 命中 {len(candidates)} 个候选")
	return candidates
