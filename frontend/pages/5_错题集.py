"""错题集页面 — 查看、管理、重做错题。"""

import sys
import pathlib
import urllib.parse
import streamlit as st
import requests

# Streamlit pages need explicit path setup to find shared/ module
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from shared.styles import apply_theme, gradient_header, glow_divider
from shared.api import get_api_base, get_public_api_base
from shared.review_session import result_at, review_summary

st.set_page_config(page_title="错题集", page_icon="📕", layout="wide")
apply_theme()

api_base = get_api_base()

gradient_header("📕 错题集", level=2)

# ── Session state init ──
for key, default in [
    ("wq_page", 1),
    ("wq_subject", "全部科目"),
    ("wq_chapter", "全部章节"),
    ("wq_status", "全部"),
    ("wq_review_mode", False),
    ("wq_review_queue", []),
    ("wq_review_data", []),
    ("wq_review_index", 0),
    ("wq_review_answered", False),
    ("wq_review_result", None),
    ("wq_review_results", {}),
    ("wq_review_drafts", {}),
    ("wq_review_mastered", set()),
    ("wq_selected", set()),
]:
    if key not in st.session_state:
        st.session_state[key] = default


# ── API helpers ──

def _get_stats():
    try:
        r = requests.get(f"{api_base}/api/wrong-questions/stats", timeout=10)
        if r.status_code == 200:
            return r.json()
    except requests.ConnectionError:
        pass
    return None


def _get_weak_points(limit=10):
    try:
        r = requests.get(
            f"{api_base}/api/weak-knowledge/weak-points",
            params={"limit": limit, "min_wrong": 1},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("items", [])
    except requests.ConnectionError:
        pass
    return []


def _create_agent_export():
    try:
        r = requests.post(f"{api_base}/api/agent-exports", timeout=60)
        if r.status_code == 200:
            return True, r.json()
        return False, r.text
    except requests.ConnectionError:
        return False, "无法连接后端"


def _get_list(subject, chapter, status, page, page_size=20):
    params = {"page": page, "page_size": page_size}
    if subject and subject != "全部科目":
        params["subject"] = subject
    if chapter and chapter != "全部章节":
        params["chapter"] = chapter
    if status and status != "全部":
        status_map = {"已掌握": "correct", "巩固中": "reviewing", "待巩固": "wrong", "未重做": "unreviewed"}
        params["status"] = status_map.get(status, status)
    try:
        r = requests.get(f"{api_base}/api/wrong-questions/", params=params, timeout=10)
        if r.status_code == 200:
            return r.json()
    except requests.ConnectionError:
        pass
    return None


def _get_chapters(subject):
    if not subject or subject == "全部科目":
        return []
    try:
        r = requests.get(
            f"{api_base}/api/wrong-questions/chapters",
            params={"subject": subject},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("chapters", [])
    except requests.ConnectionError:
        pass
    return []


def _remove(wrong_id):
    try:
        r = requests.delete(f"{api_base}/api/wrong-questions/{wrong_id}", timeout=10)
        return r.status_code == 200
    except requests.ConnectionError:
        return False


def _update_mastery(wrong_id, mastered):
    """Mark a wrong question mastered or return it to today's plan."""
    try:
        response = (requests.post if mastered else requests.delete)(
            f"{api_base}/api/wrong-questions/{wrong_id}/mastery", timeout=10
        )
        if response.status_code == 200:
            return True, None
        try:
            return False, response.json().get("detail", response.text)
        except ValueError:
            return False, response.text
    except requests.RequestException as exc:
        return False, str(exc)


def _batch_remove(wrong_ids):
    try:
        r = requests.post(
            f"{api_base}/api/wrong-questions/batch-remove",
            json={"wrong_ids": wrong_ids},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("removed", 0)
    except requests.ConnectionError:
        pass
    return 0


def _get_batch_review(wrong_ids):
    try:
        r = requests.post(
            f"{api_base}/api/wrong-questions/batch-review",
            json={"wrong_ids": wrong_ids},
            timeout=15,
        )
        if r.status_code == 200:
            return r.json().get("items", [])
    except requests.ConnectionError:
        pass
    return []


def _get_due_reviews():
    try:
        r = requests.get(f"{api_base}/api/wrong-questions/due", timeout=10)
        if r.status_code == 200:
            return r.json()
    except requests.RequestException:
        pass
    return None


def _start_review(wrong_ids):
    """Enter the existing re-challenge flow with a fixed question queue."""
    items = _get_batch_review(wrong_ids)
    if not items:
        st.warning("无法加载题目数据")
        return
    st.session_state.wq_review_mode = True
    st.session_state.wq_review_queue = wrong_ids
    st.session_state.wq_review_data = items
    st.session_state.wq_review_index = 0
    st.session_state.wq_review_answered = False
    st.session_state.wq_review_result = None
    st.session_state.wq_review_results = {}
    st.session_state.wq_review_drafts = {}
    st.session_state.wq_review_mastered = set()
    st.rerun()


def _navigate_review(index):
    """Restore this question's prior result without posting it a second time."""
    st.session_state.wq_review_index = index
    result, answered = result_at(
        st.session_state.wq_review_data, index,
        st.session_state.wq_review_results, st.session_state.wq_review_mastered,
    )
    st.session_state.wq_review_result = result
    st.session_state.wq_review_answered = answered
    st.rerun()


def _submit_review(wrong_id, user_answer):
    try:
        r = requests.post(
            f"{api_base}/api/wrong-questions/{wrong_id}/review",
            json={"user_answer": user_answer},
            timeout=60,
        )
        if r.status_code == 200:
            return r.json()
    except requests.ConnectionError:
        pass
    return None


def _self_assess_review(wrong_id, attempt_token, is_correct):
    try:
        response = requests.post(
            f"{api_base}/api/wrong-questions/{wrong_id}/self-assess",
            json={"attempt_token": attempt_token, "is_correct": is_correct},
            timeout=15,
        )
        if response.status_code == 200:
            return response.json(), None
        return None, response.json().get("detail", response.text)
    except requests.RequestException as exc:
        return None, str(exc)


def _save_review_result(wrong_id, result):
    """Save a result once per question for back/forward navigation."""
    st.session_state.wq_review_results[wrong_id] = result
    st.session_state.wq_review_result = result
    st.session_state.wq_review_answered = True
    st.rerun()


def _render_question_assets(q):
    rendered = False
    public_base = get_public_api_base()
    for asset in q.get("assets") or []:
        content = asset.get("content_md") or asset.get("text_content")
        if asset.get("asset_type") in {"table", "text"} and content:
            st.markdown(content)
            rendered = True
            continue

        path = asset.get("path")
        if path:
            img_rel = path.replace("\\", "/").strip()
            if img_rel:
                try:
                    img_col, _ = st.columns([5, 3])
                    with img_col:
                        st.image(
                            f"{public_base}/images/{urllib.parse.quote(img_rel, safe='/')}",
                            use_container_width=True,
                        )
                    rendered = True
                except Exception:
                    pass
    return rendered


def _manual_add(question_id):
    try:
        r = requests.post(
            f"{api_base}/api/wrong-questions/",
            json={"question_id": question_id},
            timeout=10,
        )
        return r.status_code == 200, r.json() if r.status_code == 200 else r.text
    except requests.ConnectionError:
        return False, "无法连接后端"


def _exit_review():
    """Clean up review session state."""
    st.session_state.wq_review_mode = False
    st.session_state.wq_review_queue = []
    st.session_state.wq_review_data = []
    st.session_state.wq_review_index = 0
    st.session_state.wq_review_answered = False
    st.session_state.wq_review_result = None
    st.session_state.wq_review_results = {}
    st.session_state.wq_review_drafts = {}
    st.session_state.wq_review_mastered = set()
    # Clean widget keys
    for k in list(st.session_state.keys()):
        if isinstance(k, str) and k.startswith("rc_"):
            del st.session_state[k]


# ── Sidebar ──

with st.sidebar:
    st.markdown(
        '<h3 class="gradient-text-sm" style="font-size:1.1rem;">🔍 筛选</h3>',
        unsafe_allow_html=True,
    )

    subjects = ["全部科目", "数据结构", "操作系统", "计算机组成原理", "计算机网络"]
    selected_subject = st.selectbox("科目", subjects, key="wq_subject_select")

    chapters = _get_chapters(selected_subject)
    chapter_options = ["全部章节"] + chapters
    selected_chapter = st.selectbox("章节", chapter_options, key="wq_chapter_select")

    status_options = ["全部", "未重做", "待巩固", "巩固中", "已掌握"]
    selected_status = st.selectbox("状态", status_options, key="wq_status_select")

    # Prominent load button
    if st.button("📋 加载错题列表", type="primary"):
        st.session_state.wq_subject = selected_subject
        st.session_state.wq_chapter = selected_chapter
        st.session_state.wq_status = selected_status
        st.session_state.wq_page = 1
        st.session_state.wq_selected = set()
        st.rerun()

    st.divider()
    st.markdown(
        '<h3 class="gradient-text-sm" style="font-size:1.1rem;">⚡ 操作</h3>',
        unsafe_allow_html=True,
    )

    # Manual add
    add_id = st.number_input(
        "手动添加题目ID", min_value=1, step=1, key="wq_add_id"
    )
    if st.button("➕ 添加到错题集"):
        ok, msg = _manual_add(int(add_id))
        if ok:
            st.success("已添加到错题集")
            st.rerun()
        else:
            st.error(f"添加失败: {msg}")

    if st.button("导出 Agent 笔记"):
        ok, payload = _create_agent_export()
        if ok:
            st.success(f"已导出：{payload.get('export_root', '')}")
        else:
            st.error(f"导出失败：{payload}")


# ── Stats Overview ──

stats = _get_stats()
if stats:
    total = stats.get("total", 0)
    by_status = stats.get("by_status", {})
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("错题总数", total)
    c2.metric("未重做", by_status.get("unreviewed", 0))
    c3.metric("已掌握 ✓", by_status.get("correct", 0))
    c4.metric("待巩固 ✗", by_status.get("wrong", 0))
    c5.metric("巩固中", by_status.get("reviewing", 0))
else:
    st.error("无法获取错题统计，请确认后端已启动")
    st.stop()


if not st.session_state.wq_review_mode:
    glow_divider()
    gradient_header("📅 今日待复习", level=2)
    due = _get_due_reviews()
    if due is None:
        st.warning("暂时无法获取复习计划，请确认后端已启动。")
    elif due["total"] == 0:
        st.success("今天暂无到期或逾期的复习题。新加入错题集的题将在 1 天后进入这里。")
    else:
        st.info(f"今天有 {due['total']} 道题待复习（含逾期）；未完成的题会留在列表中。")
        with st.expander("查看待复习题目"):
            for item in due["items"][:20]:
                due_day = (item.get("next_review_at") or "")[:10]
                st.markdown(
                    f"**{item['subject']} · #{item['question_id']}** · 原定 {due_day} · "
                    f"{item.get('question_text_preview', '')}"
                )
            if due["total"] > 20:
                st.caption(f"仅预览前 20 题，剩余 {due['total'] - 20} 题仍在待复习列表中。")
        if st.button("▶ 开始今日复习", type="primary"):
            _start_review([item["id"] for item in due["items"][:50]])
        if due["total"] > 50:
            st.caption("每次最多复习 50 题；完成后可继续开始下一组。")


weak_points = _get_weak_points(limit=5)
if weak_points:
    with st.expander("AI 薄弱点预览", expanded=False):
        for item in weak_points:
            st.markdown(f"**{item.get('knowledge_tag', '')}**")
            st.caption(
                f"错 {item.get('wrong_count', 0)} 次 · 掌握度 {item.get('mastery_score', 0):.0%}"
            )
            if item.get("ai_summary"):
                st.markdown(item["ai_summary"])
            actions = item.get("recommended_actions") or []
            if actions:
                st.markdown("复习动作：" + "；".join(actions[:3]))


# ── Re-challenge Mode ──

if st.session_state.wq_review_mode:
    glow_divider()
    st.markdown(
        '<h3 class="gradient-text-sm" style="font-size:1.3rem;margin-bottom:12px;">'
        '🔄 重做模式</h3>',
        unsafe_allow_html=True,
    )

    queue = st.session_state.wq_review_queue
    data = st.session_state.wq_review_data
    idx = st.session_state.wq_review_index
    total_q = len(data)

    # All done
    if idx >= total_q:
        summary = review_summary(st.session_state.wq_review_results, st.session_state.wq_review_mastered)
        cc, wc, uc, mc = (summary[key] for key in ("correct", "wrong", "ungraded", "manual"))
        graded = cc + wc
        st.markdown(
            f'<div class="score-card">'
            f'<div class="score-value">{cc}/{graded}</div>'
            f'<p style="color:#e0e0f0;margin-top:8px;">'
            f'正确 {cc} 题，错误 {wc} 题，未判分 {uc} 题，手动标记已掌握 {mc} 题</p></div>',
            unsafe_allow_html=True,
        )
        back_col, exit_col, _ = st.columns([1, 1, 6], gap="small")
        with back_col:
            if st.button("← 上一题", disabled=total_q == 0, use_container_width=True):
                _navigate_review(total_q - 1)
        with exit_col:
            if st.button("退出重做", type="primary", use_container_width=True):
                _exit_review()
                st.rerun()
        st.stop()

    # Progress
    st.progress(idx / total_q if total_q > 0 else 0)
    st.caption(f"第 {idx + 1} / {total_q} 题")

    # Current question
    q = data[idx]
    wid = q["wrong_id"]
    was_manually_mastered = wid in st.session_state.wq_review_mastered

    st.markdown(f"**{q.get('subject', '')}** · {q.get('chapter', '')}")
    st.markdown(q.get("question_text", ""))

    # Structured assets first, then legacy image path if needed
    if not _render_question_assets(q):
        img = q.get("image_path")
        if img:
            public_base = get_public_api_base()
            for img_rel in img.split(","):
                img_rel = img_rel.strip()
                if img_rel:
                    try:
                        img_col, _ = st.columns([5, 3])
                        with img_col:
                            st.image(f"{public_base}/images/{urllib.parse.quote(img_rel, safe='/')}", use_container_width=True)
                    except Exception:
                        pass

    # Options
    opt_map = {}
    for letter, key in [("A", "option_a"), ("B", "option_b"),
                        ("C", "option_c"), ("D", "option_d")]:
        val = q.get(key)
        if val:
            opt_map[letter] = val.strip()

    if was_manually_mastered:
        st.success("✅ 本题已手动标记为已掌握；不计入答对次数或正确率。")
    elif not st.session_state.wq_review_answered:
        # Show answer options
        if opt_map:
            letters = list(opt_map.keys())
            display_opts = [f"{L}. {opt_map[L]}" for L in letters]
            widget_key = f"rc_radio_{wid}_{idx}"
            if widget_key not in st.session_state and wid in st.session_state.wq_review_drafts:
                st.session_state[widget_key] = st.session_state.wq_review_drafts[wid]
            selected_display = st.radio(
                "选择答案",
                display_opts,
                key=widget_key,
            )
            st.session_state.wq_review_drafts[wid] = selected_display
            selected_letter = selected_display[0] if selected_display else ""

            if st.button("📝 提交答案", type="primary"):
                if selected_letter:
                    result = _submit_review(wid, selected_letter)
                    if result is not None:
                        _save_review_result(wid, result)
                    else:
                        st.error("提交失败，请检查后端连接后重试")
                else:
                    st.warning("请选择一个答案")
        else:
            # Non-choice question
            widget_key = f"rc_text_{wid}_{idx}"
            if widget_key not in st.session_state and wid in st.session_state.wq_review_drafts:
                st.session_state[widget_key] = st.session_state.wq_review_drafts[wid]
            text_ans = st.text_area("输入答案", key=widget_key)
            st.session_state.wq_review_drafts[wid] = text_ans
            if st.button("📝 提交答案", type="primary"):
                if text_ans:
                    result = _submit_review(wid, text_ans)
                    if result is not None:
                        _save_review_result(wid, result)
                    else:
                        st.error("提交失败，请检查后端连接后重试")
                else:
                    st.warning("请输入答案")
    else:
        # Show result
        result = st.session_state.wq_review_result
        if result:
            correct_answer = result.get("correct_answer", "")
            if not result.get("graded", False):
                st.info("⚪ 本题未自动判分；自评前不计入记录，错题状态保持不变。请自行对照参考答案。")
                if correct_answer and correct_answer != "(暂无答案)":
                    with st.expander("📖 查看参考答案"):
                        st.markdown(correct_answer)
                elif result.get("answer_ref"):
                    st.caption(f"答案参考: {result['answer_ref']}")
                if result.get("analysis"):
                    with st.expander("📖 查看解析"):
                        st.markdown(result["analysis"])
                if result.get("attempt_token"):
                    st.caption("对照参考答案或原书答案后，请自行判断对错。")
                    yes, no, _ = st.columns([1, 1, 6], gap="small")
                    chosen = None
                    with yes:
                        if st.button("✅ 我答对了", key=f"review_yes_{wid}_{idx}"):
                            chosen = True
                    with no:
                        if st.button("❌ 我答错了", key=f"review_no_{wid}_{idx}"):
                            chosen = False
                    if chosen is not None:
                        assessed, error = _self_assess_review(wid, result["attempt_token"], chosen)
                        if assessed:
                            _save_review_result(wid, assessed)
                        else:
                            st.error(f"自评保存失败：{error}")
            elif result.get("assessment_source") == "self_assessed":
                st.success("✅ 已自评：答对" if result["is_correct"] else "❌ 已自评：答错")
                st.caption("结果来自用户自评，不是自动判分。")
                if correct_answer != "(暂无答案)":
                    with st.expander("📖 查看参考答案"):
                        st.markdown(correct_answer)
            elif result.get("is_correct"):
                st.success(f"✅ 回答正确！正确答案: {result.get('correct_answer', '')}")
            else:
                st.error(
                    f"❌ 回答错误！你的答案: {result.get('user_answer', '')}，"
                    f"正确答案: {result.get('correct_answer', '')}"
                )
            if result.get("graded") and result.get("analysis"):
                with st.expander("📖 查看解析"):
                    st.markdown(result["analysis"])

    # Manual mastery is available before answering or after an ungraded result;
    # an objectively graded attempt should not be silently overridden here.
    may_mark = (q.get("last_status") != "correct" and not was_manually_mastered and
                (not st.session_state.wq_review_answered or
                 not (st.session_state.wq_review_result or {}).get("graded", False)))
    nav_prev, nav_next, nav_master, nav_exit, _ = st.columns([1, 1, 1, 1, 4], gap="small")
    with nav_prev:
        if st.button("← 上一题", disabled=idx == 0, use_container_width=True):
            _navigate_review(idx - 1)
    with nav_next:
        if st.button("下一题 →", type="primary", use_container_width=True):
            _navigate_review(idx + 1)
    with nav_master:
        if may_mark and st.button("✅ 标记掌握", key=f"review_master_{wid}_{idx}", help="标记已掌握，不计入答对次数", use_container_width=True):
            ok, error = _update_mastery(wid, True)
            if ok:
                st.session_state.wq_review_mastered.add(wid)
                _navigate_review(idx + 1)
            st.error(f"标记失败：{error}")
    with nav_exit:
        if st.button("退出重做", use_container_width=True):
            _exit_review()
            st.rerun()

    st.stop()


# ── Main: Question List ──

glow_divider()

# Batch action bar
sel = st.session_state.wq_selected
if sel:
    action_col1, action_col2, action_col3 = st.columns([1, 1, 3])
    with action_col1:
        if st.button(f"🔄 重做选中 ({len(sel)})", type="primary"):
            _start_review(list(sel))
    with action_col2:
        if st.button(f"🗑️ 移除选中 ({len(sel)})"):
            count = _batch_remove(list(sel))
            st.session_state.wq_selected = set()
            st.success(f"已移除 {count} 题")
            st.rerun()

# Fetch list
result = _get_list(
    st.session_state.wq_subject,
    st.session_state.wq_chapter,
    st.session_state.wq_status,
    st.session_state.wq_page,
)

if not result:
    st.info("暂无错题数据")
    st.stop()

items = result.get("items", [])
total = result.get("total", 0)
pages = result.get("pages", 1)
current_page = result.get("page", 1)

if not items:
    st.markdown(
        '<div class="neon-card" style="text-align:center;padding:40px;">'
        '<p style="color:#7878a0;font-size:1.1rem;">🎉 暂无错题</p>'
        '<p style="color:#7878a0;">刷题答错后会自动收录到这里</p>'
        '</div>',
        unsafe_allow_html=True,
    )
    st.stop()

st.caption(f"共 {total} 题 · 第 {current_page}/{pages} 页")

# Render each item
status_class = {"correct": "wq-correct", "wrong": "wq-wrong", "reviewing": "wq-reviewing", "unreviewed": "wq-unreviewed"}
dot_class = {"correct": "wq-dot-correct", "wrong": "wq-dot-wrong", "reviewing": "wq-dot-reviewing", "unreviewed": "wq-dot-unreviewed"}
status_label = {"correct": "已掌握", "wrong": "待巩固", "reviewing": "巩固中", "unreviewed": "未重做"}

for item in items:
    wid = item["id"]
    ls = item.get("last_status", "unreviewed")
    cls = status_class.get(ls, "wq-unreviewed")
    dcls = dot_class.get(ls, "wq-dot-unreviewed")
    subject = item.get("subject", "")
    chapter = item.get("chapter", "")
    preview = item.get("question_text_preview", "")
    reviews = item.get("review_count", 0)

    # Checkbox for selection
    checked = st.checkbox(
        "select",
        key=f"wq_chk_{wid}",
        value=(wid in st.session_state.wq_selected),
        label_visibility="collapsed",
    )
    if checked:
        st.session_state.wq_selected.add(wid)
    else:
        st.session_state.wq_selected.discard(wid)

    # Card HTML
    st.markdown(
        f'<div class="wq-item {cls}" style="margin-left:36px;">'
        f'<span class="wq-status-dot {dcls}"></span>'
        f'<span class="wq-subject-tag">{subject}</span>'
        f'<span style="color:#7878a0;font-size:0.85rem;">{chapter}</span>'
        f'<p style="color:#e0e0f0;margin:8px 0 4px 0;">{preview}</p>'
        f'<span style="color:#7878a0;font-size:0.8rem;">'
        f'重做 {reviews} 次 · '
        f'{status_label.get(ls, "未重做")}'
        f'{"（手动标记）" if ls == "correct" and item.get("mastery_source") == "manual" else ""}'
        f'</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # Action buttons
    btn_col1, btn_col2, btn_col3, _ = st.columns([1, 1, 1, 5], gap="small")
    with btn_col1:
        if st.button("🔄 重做", key=f"wq_do_{wid}", use_container_width=True):
            _start_review([wid])
    with btn_col2:
        if ls == "correct":
            if st.button("↩ 重新复习", key=f"wq_rejoin_{wid}", help="退出已掌握，1 天后开始首次复习", use_container_width=True):
                ok, error = _update_mastery(wid, False)
                if ok:
                    st.rerun()
                st.error(f"重新加入失败：{error}")
        elif st.button("✅ 标记掌握", key=f"wq_master_{wid}", help="标记已掌握，不计入答对次数", use_container_width=True):
            ok, error = _update_mastery(wid, True)
            if ok:
                st.rerun()
            st.error(f"标记失败：{error}")
    with btn_col3:
        if st.button("🗑️ 移除", key=f"wq_rm_{wid}", use_container_width=True):
            if _remove(wid):
                st.session_state.wq_selected.discard(wid)
                st.rerun()

# ── Pagination ──
if pages > 1:
    st.markdown("---")
    pg_col1, pg_col2, pg_col3 = st.columns([1, 2, 1])
    with pg_col1:
        if st.button("← 上一页", disabled=(current_page <= 1)):
            st.session_state.wq_page = current_page - 1
            st.rerun()
    with pg_col2:
        st.markdown(
            f'<p style="text-align:center;color:#7878a0;">'
            f'第 {current_page} / {pages} 页</p>',
            unsafe_allow_html=True,
        )
    with pg_col3:
        if st.button("下一页 →", disabled=(current_page >= pages)):
            st.session_state.wq_page = current_page + 1
            st.rerun()
