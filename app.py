from datetime import date, datetime, timezone
import json
import logging
from pathlib import Path
import time
from typing import Any

import streamlit as st

from config import Settings
from exporters import export_to_csv, export_to_json, export_to_markdown
from github_client import GitHubAPIError, GitHubClient
from models import Bookmark, Repository, SearchReport
from scoring import score_repository
from search_engine import SearchEngine, SearchFilters, build_query
from storage import StorageManager

# Set page configuration
st.set_page_config(
    page_title="GitHub Project Hunter",
    page_icon="⚡ è",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom styling
st.markdown(
    """
    <style>
    .main-header {
        font-size: 2.2rem;
        font-weight: 800;
        background: linear-gradient(90deg, #3b82f6, #8b5cf6, #ec4899);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        margin-bottom: 0.2rem;
    }
    .sub-header {
        color: #94a3b8;
        font-size: 1.05rem;
        margin-bottom: 1.5rem;
    }
    .metric-card {
        background-color: #1e293b;
        border-radius: 10px;
        padding: 15px;
        border: 1px solid #334155;
        text-align: center;
    }
    .badge-grade-aplus { background-color: #059669; color: white; padding: 3px 10px; border-radius: 12px; font-weight: bold; }
    .badge-grade-a { background-color: #10b981; color: white; padding: 3px 10px; border-radius: 12px; font-weight: bold; }
    .badge-grade-b { background-color: #3b82f6; color: white; padding: 3px 10px; border-radius: 12px; font-weight: bold; }
    .badge-grade-c { background-color: #f59e0b; color: white; padding: 3px 10px; border-radius: 12px; font-weight: bold; }
    .badge-grade-d { background-color: #f97316; color: white; padding: 3px 10px; border-radius: 12px; font-weight: bold; }
    .badge-grade-f { background-color: #ef4444; color: white; padding: 3px 10px; border-radius: 12px; font-weight: bold; }
    .repo-card {
        background: #0f172a;
        border: 1px solid #1e293b;
        border-radius: 12px;
        padding: 20px;
        margin-bottom: 16px;
        transition: transform 0.15s ease, border-color 0.15s ease;
    }
    .repo-card:hover {
        border-color: #3b82f6;
    }
    .tag-chip {
        background-color: #1e293b;
        color: #cbd5e1;
        padding: 2px 8px;
        border-radius: 6px;
        font-size: 0.8rem;
        margin-right: 5px;
        display: inline-block;
        margin-bottom: 4px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource
def get_settings():
    return Settings.from_env()


@st.cache_resource
def get_storage():
    settings = get_settings()
    return StorageManager(data_dir=settings.data_dir)


def get_grade_badge(grade: str) -> str:
    badge_map = {
        "A+": "badge-grade-aplus",
        "A": "badge-grade-a",
        "B": "badge-grade-b",
        "C": "badge-grade-c",
        "D": "badge-grade-d",
        "F": "badge-grade-f",
    }
    cls = badge_map.get(grade, "badge-grade-b")
    return f'<span class="{cls}">{grade}</span>'


# Initialize session state variables
if "search_report" not in st.session_state:
    st.session_state.search_report = None
if "search_query_used" not in st.session_state:
    st.session_state.search_query_used = ""
if "is_searching" not in st.session_state:
    st.session_state.is_searching = False


# Sidebar Configuration & Controls
with st.sidebar:
    st.image("https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png", width=48)
    st.markdown("###  Search Parameters")

    # Primary idea input
    idea_input = st.text_area(
        "Project Idea / Problem Statement",
        placeholder="e.g. AI-powered medical diagnosis simulator using computer vision",
        height=90,
        help="Natural language description of what you are hunting for.",
    )

    keywords_input = st.text_input(
        "Explicit Keywords (Optional)",
        placeholder="e.g. medical simulation vision",
        help="Space or comma-separated terms to override automatic term extraction.",
    )

    # Core filters
    st.markdown("---")
    st.markdown("#### 🔬 Filtering")

    popular_langs = [
        "Any",
        "Python",
        "TypeScript",
        "JavaScript",
        "Rust",
        "Go",
        "Java",
        "C++",
        "C#",
        "PHP",
        "Ruby",
        "Swift",
        "Kotlin",
    ]
    lang_choice = st.selectbox("Primary Language", popular_langs, index=0)
    language_filter = None if lang_choice == "Any" else lang_choice

    col_s1, col_s2 = st.columns(2)
    with col_s1:
        min_stars = st.number_input("Min Stars", min_value=0, max_value=500000, value=10, step=10)
    with col_s2:
        max_stars_input = st.number_input("Max Stars (0=None)", min_value=0, max_value=500000, value=0, step=50)
        max_stars = None if max_stars_input == 0 else int(max_stars_input)

    min_forks = st.number_input("Min Forks", min_value=0, max_value=50000, value=0, step=5)

    popular_licenses = [
        "Any",
        "mit",
        "apache-2.0",
        "gpl-3.0",
        "bsd-3-clause",
        "bsd-2-clause",
        "mpl-2.0",
        "lgpl-3.0",
        "agpl-3.0",
        "unlicense",
    ]
    license_choice = st.selectbox("License", popular_licenses, index=0)
    license_filter = None if license_choice == "Any" else license_choice

    topics_input = st.text_input("Topics / Tags (comma separated)", placeholder="e.g. deep-learning, pytorch")
    topics_list = tuple(
        t.strip().lower() for t in topics_input.split(",") if t.strip()
    ) if topics_input else ()

    with st.expander(" Date & Origin Filters"):
        col_d1, col_d2 = st.columns(2)
        with col_d1:
            created_after = st.date_input("Created After", value=None)
            pushed_after = st.date_input("Pushed After", value=None)
        with col_d2:
            created_before = st.date_input("Created Before", value=None)
            pushed_before = st.date_input("Pushed Before", value=None)

        include_forks = st.checkbox("Include Forks", value=False)
        include_archived = st.checkbox("Include Archived", value=False)

    st.markdown("---")
    st.markdown("#### ⚙️ Engine Scan Budget")

    sort_options = {
        "quality-score": "Quality Score (Heuristic)",
        "stars": "Stars (Highest first)",
        "forks": "Forks (Highest first)",
        "updated": "Recently Pushed",
        "best-match": "GitHub Best Match",
    }
    sort_choice = st.selectbox(
        "Sort Criteria",
        options=list(sort_options.keys()),
        format_func=lambda k: sort_options[k],
        index=0,
    )

    col_b1, col_b2 = st.columns(2)
    with col_b1:
        max_results = st.slider("Max Results", 5, 100, 20, step=5)
    with col_b2:
        scan_limit = st.slider("Candidate Budget", 20, 1000, 200, step=20)

    enrich_readme = st.checkbox("Fetch README Previews", value=False, help="Fetches README for discovery cards (adds requests)")

    search_button = st.button("🚀 Hunt Projects", type="primary", use_container_width=True)


# Main Content Area
st.markdown('<div class="main-header">⚡ GitHub Project Hunter</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="sub-header">Modern repository discovery engine with multi-factor quality scoring, real-time scanning & project bookmarks.</div>',
    unsafe_allow_html=True,
)

settings = get_settings()
storage = get_storage()

# API Rate Limit & Status Quick Strip
with st.container():
    col_stat1, col_stat2, col_stat3, col_stat4 = st.columns(4)
    with col_stat1:
        auth_status = "🔑 Authenticated" if settings.token else "🌐 Unauthenticated"
        st.metric("Auth Mode", auth_status, help="Set GITHUB_TOKEN in .env for 5,000 req/hr")
    with col_stat2:
        st.metric("Cache Mode", "L1 Mem + L2 SQLite" if settings.persistent_cache_enabled else "Memory Only")
    with col_stat3:
        bm_count = len(storage.list_bookmarks())
        st.metric("Saved Bookmarks", bm_count)
    with col_stat4:
        hist_count = len(storage.list_history())
        st.metric("Search History", hist_count)

st.write("")

# Navigation Tabs
tab_hunt, tab_bookmarks, tab_history, tab_diagnostics = st.tabs(
    ["🔍 Discover & Hunt", "⭐ Bookmarks & Favorites", "📜 Search History", "⚙️ Diagnostics & API Quota"]
)


# ==========================================
# TAB 1: DISCOVER & HUNT
# ==========================================
with tab_hunt:
    # If search button clicked
    if search_button:
        if not idea_input.strip():
            st.warning("⚠️ Please provide a project idea or problem statement in the sidebar.")
        else:
            try:
                # Prepare filters
                filters = SearchFilters(
                    language=language_filter,
                    min_stars=int(min_stars),
                    max_stars=max_stars,
                    min_forks=int(min_forks),
                    license_key=license_filter,
                    topics=topics_list,
                    created_after=created_after,
                    created_before=created_before,
                    pushed_after=pushed_after,
                    pushed_before=pushed_before,
                    include_archived=include_archived,
                    include_forks=include_forks,
                )

                keywords_terms = tuple(k.strip() for k in keywords_input.replace(",", " ").split() if k.strip())

                # Progress placeholders
                progress_container = st.container()
                with progress_container:
                    progress_bar = st.progress(0, text="Initializing search engine...")
                    status_text = st.empty()

                def update_progress(scanned: int, matched: int, budget: int, msg: str):
                    pct = min(1.0, max(0.0, scanned / max(1, budget)))
                    progress_bar.progress(pct, text=f"Inspecting candidates... ({scanned}/{budget})")
                    status_text.markdown(f"**Status:** `{msg}` | Found `{matched}` matching target results.")

                with GitHubClient(settings) as client:
                    engine = SearchEngine(client)
                    report = engine.search(
                        idea_input.strip(),
                        filters,
                        keywords=keywords_terms,
                        max_results=max_results,
                        scan_limit=scan_limit,
                        sort=sort_choice,
                        on_progress=update_progress,
                        score_results=True,
                        enrich_readme=enrich_readme,
                    )

                progress_container.empty()
                st.session_state.search_report = report
                st.session_state.search_query_used = report.query

                # Save to history
                storage.add_history_entry(
                    idea=idea_input.strip(),
                    keywords=keywords_terms,
                    filters={
                        "language": language_filter,
                        "min_stars": int(min_stars),
                        "sort": sort_choice,
                    },
                    result_count=len(report.repositories),
                    total_count=report.total_count,
                )
                st.success(f"✨ Search complete! Discovered **{len(report.repositories)}** repositories in {report.duration_seconds:.2f}s.")

            except Exception as exc:
                st.error(f"❌ Search failed: {exc}")

    # Display Search Results if available
    report: SearchReport | None = st.session_state.search_report
    if report:
        st.markdown("### 📊 Scan Results Overview")

        col_m1, col_m2, col_m3, col_m4, col_m5 = st.columns(5)
        with col_m1:
            st.metric("Discovered", len(report.repositories))
        with col_m2:
            st.metric("Candidates Scanned", f"{report.scanned} / {scan_limit}")
        with col_m3:
            st.metric("GitHub Total Matches", f"{report.total_count or 0:,}")
        with col_m4:
            st.metric("Pages Fetched", report.pages_fetched)
        with col_m5:
            st.metric("Scan Time", f"{report.duration_seconds:.2f}s")

        if report.warnings:
            with st.expander(" Scan Warnings & Diagnostics", expanded=False):
                for w in report.warnings:
                    st.warning(w)

        with st.expander(" Emitted GitHub API Query", expanded=False):
            st.code(report.query, language="text")
            st.caption(f"Lexical terms used: {', '.join(report.search_terms)}")

        st.markdown("---")

        # Result filtering and export bar
        col_ctrl1, col_ctrl2, col_ctrl3, col_ctrl4 = st.columns([2, 1, 1, 1])

        with col_ctrl1:
            search_within = st.text_input("Filter results list", placeholder="Search by name, description, topic...", label_visibility="collapsed")
        with col_ctrl2:
            grade_filter = st.selectbox(
                "Filter Grade",
                ["All Grades", "A+ & A only", "B and above", "C and above"],
                index=0,
                label_visibility="collapsed",
            )
        with col_ctrl3:
            view_mode = st.radio("View", ["Cards", "Table"], horizontal=True, label_visibility="collapsed")
        with col_ctrl4:
            export_format = st.selectbox("Export", ["Download CSV", "Download JSON", "Download Markdown"], label_visibility="collapsed")

        # Download buttons
        if export_format == "Download CSV":
            csv_data = export_to_csv(report.repositories)
            st.download_button(
                "📥 Download CSV",
                data=csv_data,
                file_name=f"github_hunter_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
                use_container_width=True,
            )
        elif export_format == "Download JSON":
            json_data = export_to_json(report)
            st.download_button(
                "📥 Download JSON",
                data=json_data,
                file_name=f"github_hunter_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                mime="application/json",
                use_container_width=True,
            )
        else:
            md_data = export_to_markdown(report.repositories)
            st.download_button(
                "📥 Download Markdown",
                data=md_data,
                file_name=f"github_hunter_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md",
                mime="text/markdown",
                use_container_width=True,
            )

        # Filter the repository list locally
        filtered_repos = []
        for r in report.repositories:
            if search_within:
                q = search_within.lower()
                text_blob = f"{r.full_name} {r.description or ''} {' '.join(r.topics)} {r.language or ''}".lower()
                if q not in text_blob:
                    continue
            if grade_filter == "A+ & A only" and (not r.quality_score or r.quality_score.grade not in ("A+", "A")):
                continue
            if grade_filter == "B and above" and (not r.quality_score or r.quality_score.grade not in ("A+", "A", "B")):
                continue
            if grade_filter == "C and above" and (not r.quality_score or r.quality_score.grade not in ("A+", "A", "B", "C")):
                continue
            filtered_repos.append(r)

        st.caption(f"Showing **{len(filtered_repos)}** of {len(report.repositories)} repositories.")

        # Display either Table or Cards
        if view_mode == "Table":
            import pandas as pd
            table_data = [r.to_flat_dict() for r in filtered_repos]
            if table_data:
                df = pd.DataFrame(table_data)
                # Reorder columns
                display_cols = ["quality_score", "grade", "full_name", "stars", "forks", "language", "license", "recommendation", "pushed_at", "url"]
                available_cols = [c for c in display_cols if c in df.columns]
                st.dataframe(df[available_cols], use_container_width=True, hide_index=True)
            else:
                st.info("No repositories match the current filter.")
        else:
            # Cards View
            if not filtered_repos:
                st.info("No repositories match the current filter.")
            else:
                for idx, repo in enumerate(filtered_repos):
                    score = repo.quality_score
                    score_val = score.overall_score if score else 0
                    grade_val = score.grade if score else "N/A"
                    badge_html = get_grade_badge(grade_val)

                    with st.container():
                        st.markdown(
                            f"""
                            <div class="repo-card">
                                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                                    <h3 style="margin: 0; font-size: 1.3rem;">
                                        <a href="{repo.url}" target="_blank" style="text-decoration: none; color: #60a5fa;">{repo.full_name}</a>
                                    </h3>
                                    <div>
                                        {badge_html}
                                        <span style="font-size: 1.1rem; font-weight: bold; margin-left: 8px; color: #f8fafc;">{score_val}/100</span>
                                    </div>
                                </div>
                                <p style="color: #cbd5e1; font-size: 0.95rem; margin-bottom: 10px;">{repo.description or '<em>No description provided.</em>'}</p>
                                <div style="margin-bottom: 10px;">
                                    <span class="tag-chip">⭐ {repo.stars or 0:,} stars</span>
                                    <span class="tag-chip">🍴 {repo.forks or 0:,} forks</span>
                                    <span class="tag-chip">💻 {repo.language or 'Unknown'}</span>
                                    <span class="tag-chip">📜 {repo.license_spdx or repo.license_key or 'No License'}</span>
                                    <span class="tag-chip">🕒 Pushed: {repo.pushed_at.strftime('%Y-%m-%d') if repo.pushed_at else 'Unknown'}</span>
                                </div>
                                <div>
                                    {''.join(f'<span class="tag-chip" style="background-color: #1e3a8a; color: #93c5fd;">#{t}</span>' for t in repo.topics[:8])}
                                </div>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )

                        # Expandable Details & Actions
                        col_card_act1, col_card_act2 = st.columns([4, 1])
                        with col_card_act1:
                            with st.expander(f"ℹ️ Score Breakdown & Details for {repo.name}", expanded=False):
                                if score:
                                    col_sc1, col_sc2, col_sc3, col_sc4 = st.columns(4)
                                    with col_sc1:
                                        st.metric("Activity", f"{score.activity_score}/30")
                                    with col_sc2:
                                        st.metric("Popularity", f"{score.popularity_score}/30")
                                    with col_sc3:
                                        st.metric("Documentation", f"{score.documentation_score}/25")
                                    with col_sc4:
                                        st.metric("Health", f"{score.health_score}/15")

                                    st.markdown(f"**Recommendation:** `{score.recommendation}`")
                                    st.markdown("**Assessment Signals:**")
                                    for r_signal in score.reasons:
                                        st.markdown(f"- {r_signal}")

                                if repo.readme_preview:
                                    st.markdown("---")
                                    st.markdown("#### README Preview")
                                    st.markdown(repo.readme_preview)

                        with col_card_act2:
                            is_bmed = storage.is_bookmarked(repo.id)
                            bm_label = "✅ Bookmarked" if is_bmed else "⭐ Bookmark"
                            if st.button(bm_label, key=f"bm_btn_{repo.id}_{idx}", disabled=is_bmed):
                                storage.add_bookmark(repo, notes="Discovered via Hunter")
                                st.toast(f"Saved {repo.name} to bookmarks!", icon="⭐")
                                st.rerun()

                        st.write("")
    else:
        st.info("💡 Enter your project idea in the left sidebar and click **🚀 Hunt Projects** to begin discovery.")


# ==========================================
# TAB 2: BOOKMARKS & FAVORITES
# ==========================================
with tab_bookmarks:
    st.markdown("### ⭐ Saved Repository Bookmarks")

    bookmarks = storage.list_bookmarks()
    if not bookmarks:
        st.info("You haven't saved any bookmarks yet. Discovered projects can be saved with one click from the Discover tab!")
    else:
        col_b_head1, col_b_head2, col_b_head3 = st.columns([3, 1, 1])
        with col_b_head1:
            bm_search = st.text_input("Filter bookmarks", placeholder="Search in names, notes, or tags...")
        with col_b_head2:
            bm_csv = export_to_csv(bookmarks)
            st.download_button("📥 Export CSV", data=bm_csv, file_name="github_hunter_bookmarks.csv", mime="text/csv", use_container_width=True)
        with col_b_head3:
            if st.button("🗑️ Clear All", help="Clear all saved bookmarks"):
                storage.clear_bookmarks()
                st.rerun()

        filtered_bms = storage.list_bookmarks(query=bm_search if bm_search else None)
        st.caption(f"Showing **{len(filtered_bms)}** saved repositories.")

        for bm in filtered_bms:
            with st.container():
                st.markdown(
                    f"""
                    <div class="repo-card">
                        <div style="display: flex; justify-content: space-between; align-items: center;">
                            <h3 style="margin: 0;">
                                <a href="{bm.url}" target="_blank" style="text-decoration: none; color: #60a5fa;">{bm.full_name}</a>
                            </h3>
                            <div>
                                {get_grade_badge(bm.grade or 'B')}
                                <span style="font-size: 1.1rem; font-weight: bold; margin-left: 8px;">{bm.score or '-'}/100</span>
                            </div>
                        </div>
                        <p style="color: #cbd5e1; font-size: 0.95rem; margin: 8px 0;">{bm.description or 'No description'}</p>
                        <div>
                            <span class="tag-chip">⭐ {bm.stars or 0:,} stars</span>
                            <span class="tag-chip">🍴 {bm.forks or 0:,} forks</span>
                            <span class="tag-chip">💻 {bm.language or 'Unknown'}</span>
                            <span class="tag-chip">📜 {bm.license or 'None'}</span>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

                col_edit_notes, col_del_bm = st.columns([5, 1])
                with col_edit_notes:
                    new_notes = st.text_input("Notes", value=bm.notes, key=f"notes_{bm.repo_id}")
                    if new_notes != bm.notes:
                        storage.update_bookmark_notes(bm.repo_id, new_notes)
                        st.toast("Notes updated!", icon="📝")
                with col_del_bm:
                    if st.button("Remove", key=f"del_bm_{bm.repo_id}"):
                        storage.remove_bookmark(bm.repo_id)
                        st.rerun()
                st.write("")


# ==========================================
# TAB 3: SEARCH HISTORY
# ==========================================
with tab_history:
    st.markdown("### 📜 Past Searches & Queries")
    history = storage.list_history(limit=50)

    if not history:
        st.info("No past searches recorded yet.")
    else:
        if st.button("🗑️ Clear Search History"):
            storage.clear_history()
            st.rerun()

        for item in history:
            with st.container():
                col_h1, col_h2, col_h3 = st.columns([3, 1, 1])
                with col_h1:
                    st.markdown(f"**Idea:** {item.idea}")
                    if item.keywords:
                        st.caption(f"Keywords: {', '.join(item.keywords)}")
                with col_h2:
                    st.metric("Results", item.result_count)
                with col_h3:
                    st.caption(f"Time: {item.timestamp[:19].replace('T', ' ')}")
                st.markdown("---")


# ==========================================
# TAB 4: SYSTEM DIAGNOSTICS & API QUOTA
# ==========================================
with tab_diagnostics:
    st.markdown("### ⚙️ System Status & API Monitoring")

    with GitHubClient(settings) as client:
        rate_info = client.get_rate_limit()

    if rate_info and "resources" in rate_info:
        core_rate = rate_info["resources"].get("core", {})
        search_rate = rate_info["resources"].get("search", {})

        col_r1, col_r2 = st.columns(2)
        with col_r1:
            st.markdown("#### 📡 Core API (Repositories, READMEs)")
            core_rem = core_rate.get("remaining", 0)
            core_limit = core_rate.get("limit", 60)
            st.progress(core_rem / max(1, core_limit), text=f"Remaining: {core_rem:,} / {core_limit:,}")
            core_reset = datetime.fromtimestamp(core_rate.get("reset", 0), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            st.caption(f"Quota resets at: {core_reset}")

        with col_r2:
            st.markdown("#### 🔍 Search API (Repo Search)")
            search_rem = search_rate.get("remaining", 0)
            search_limit = search_rate.get("limit", 10)
            st.progress(search_rem / max(1, search_limit), text=f"Remaining: {search_rem:,} / {search_limit:,}")
            search_reset = datetime.fromtimestamp(search_rate.get("reset", 0), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            st.caption(f"Quota resets at: {search_reset}")

    st.markdown("---")
    st.markdown("#### 💾 Persistent Cache Status")
    cache_path = Path(settings.cache_dir) / "github_cache.db"
    if cache_path.exists():
        from cache import PersistentCache
        pc = PersistentCache(cache_dir=settings.cache_dir)
        st.write(f"- Cache Location: `{cache_path.resolve()}`")
        st.write(f"- Active Cached Responses: **{pc.count()}**")
        if st.button("🧹 Flush Persistent Cache"):
            flushed = pc.clear()
            st.success(f"Cleared {flushed} cached responses!")
            st.rerun()
    else:
        st.info("Persistent cache DB is empty or will be created on the first API request.")
