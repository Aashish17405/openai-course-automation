"""Smoke test: sidebar safety, curriculum-hint validation, drag-to-reorder matching, multi-question assessment flow. Run: uv run python tests/smoke_sidebar.py"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402
from src.agent.analyzer import AnalysisResponse  # noqa: E402
from src.agent.browser_controller import BrowserController  # noqa: E402
from src.agent.runner import CourseRunner  # noqa: E402

SIDEBAR_ITEMS = """
<ul>
  <li><a href="#" onclick="window.sb=(window.sb||0)+1;return false">Lesson 1.1: Intro</a><svg class="icon-lock" width="10" height="10"></svg></li>
  <li><a href="#" onclick="window.sb=(window.sb||0)+1;return false">LESSON 2.3: WHY HUMAN REVIEW MATTERS</a></li>
  <li><a href="#" onclick="window.sb=(window.sb||0)+1;return false">Continue</a></li>
  <li>Progress 20%</li>
</ul>"""

MAIN = """
<main style="margin-left:320px;padding:20px">
  <div class="block">Question 02/20</div>
  <p>Which of these is a sensible first task to try with ChatGPT at work?</p>
  <label class="block"><input type="checkbox"> Alpha option text here</label><br>
  <label class="block"><input type="checkbox"> Gamma option text here</label><br>
  <div onclick="window.bad=(window.bad||0)+1">Best next step</div>
  <button onclick="window.nxt=(window.nxt||0)+1">Next</button>
  <div style="height:900px"></div>
  <button id="pill" onclick="window.main=(window.main||0)+1">LESSON 2.3: WHY HUMAN REVIEW MATTERS</button>
</main>"""

RAIL = '<div style="position:fixed;left:0;top:0;width:280px;height:100vh;overflow:auto;background:#eee">%s</div>'
VARIANTS = {"aside-tag": '<aside style="position:fixed;left:0;top:0;width:280px">%s</aside>' % SIDEBAR_ITEMS, "layout-only rail": RAIL % SIDEBAR_ITEMS}


async def run_variant(page, name, sidebar_html) -> int:
    await page.set_content(f"<html><body style='margin:0'>{sidebar_html}{MAIN}</body></html>")
    await page.evaluate("window.main = window.nxt = window.bad = window.sb = 0")  # globals survive set_content
    ctl = BrowserController()
    ctl.page = page
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    dom = await ctl.extract_interactive_dom()
    texts = [b["text"] for b in dom["buttons"]]
    check(not any("1.1" in t for t in texts), "sidebar link leaked into buttons")
    check("Lesson 1.1" not in dom["bodyTextPreview"], "sidebar text leaked into bodyTextPreview")
    check((dom["primaryProgressionButton"] or {}).get("text", "").startswith("LESSON 2.3"), "primary button not from main")
    check(dom["isProgressionLocked"] is False, "sidebar lock icon / 'block' class marked page locked")
    check(dom["sidebarIgnored"] >= 1, "sidebar not detected")

    check(await ctl.click_by_text("Lesson 1.1: Intro") is False, "clicked a sidebar-only item")
    check(await ctl.click_by_text("LESSON 2.3: WHY HUMAN REVIEW MATTERS", require_enabled=True), "main pill not clicked")
    check(await page.evaluate("window.main") == 1, "main pill click count != 1")
    check(await ctl.click_at_point(50, 50) is False, "coordinate click in sidebar was allowed")

    await ctl.click_by_text("Next", require_enabled=True)
    check(await page.evaluate("window.nxt") == 1 and not await page.evaluate("window.bad"), "'Next' hit 'Best next step'")

    await page.evaluate("window.scrollTo(0,0)")
    await ctl.select_quiz_option("Gamma option text here")
    await ctl.select_quiz_option("Gamma option text here")  # second call must NOT toggle it off
    checked = await page.evaluate("[...document.querySelectorAll('input')].map(i => i.checked)")
    check(checked == [False, True], f"checkbox state wrong: {checked}")

    check(not await page.evaluate("window.sb"), "sidebar received clicks")
    print(f"[{name}] " + ("PASS" if not failures else "FAIL: " + "; ".join(failures)))
    return len(failures)


async def run_hint_case(page) -> int:
    """'team offsite' triggers a canned answer that is NOT on screen: it must be ignored in favour of the model's pick."""
    await page.set_content("""<body><div>Question 02/20</div><p>Plan the team offsite agenda. Which option fits best here?</p>
      <label><input type="checkbox"> Alpha option text here</label><br><label><input type="checkbox"> Gamma option text here</label></body>""")
    ctl = BrowserController()
    ctl.page = page
    runner = CourseRunner(controller=ctl, analyzer=None)
    runner.current_dom_data = await ctl.extract_interactive_dom()
    analysis = AnalysisResponse.model_validate({
        "detected_task_type": "SINGLE_SELECT_MCQ", "page_state": "QUIZ_QUESTION", "observations": "quiz",
        "quiz": {"question_text": "Plan the team offsite agenda", "selected_option_texts": ["Gamma option text here"], "explanation": "x"},
        "action": {"action_type": "SELECT_QUIZ_OPTION", "explanation": "x"}})
    await runner.execute_action(analysis)
    checked = await page.evaluate("[...document.querySelectorAll('input')].map(i => i.checked)")
    ok = checked == [False, True]
    print("[curriculum hint] " + ("PASS" if ok else f"FAIL: model pick overridden by off-screen canned answer, state={checked}"))
    return 0 if ok else 1


DRAG_PAGE = """<body style="margin:0;font:16px sans-serif">
<h2>Match each routing item to the strongest decision layer.</h2><h4>Match the pairs</h4>
<div style="margin:6px 0">Drag the cards on the right until every requirement lines up with its layer.</div>
<div style="display:flex;width:1000px">
  <div id="L" style="width:440px">%(left)s</div>
  <div id="M" style="width:60px">%(eqs)s</div>
  <div id="R" style="width:440px">%(right)s</div>
</div>
<button>Submit</button> or press Enter
<script>
function sortable(id){const col=document.getElementById(id);let drag=null;
  col.addEventListener('pointerdown',e=>{drag=e.target.closest('.card');});
  window.addEventListener('pointerup',e=>{if(!drag)return;
    const others=[...col.children].filter(x=>x!==drag);
    const idx=others.filter(x=>{const r=x.getBoundingClientRect();return r.top+r.height/2<e.clientY;}).length;
    col.insertBefore(drag,others[idx]||null);drag=null;});}
%(sortables)s
</script></body>"""
LEFT = ["A customer-managed open-weight model required after hosted options are evaluated", "Migration discovery",
        "A Workspace Agent that uses apps, actions, schedules, and approvals", "ChatGPT Work for delegated knowledge work", "Code review and refactoring help"]
RIGHT_SCRAMBLED = ["User or product surface", "Codex", "Deployment and operating model", "Capability or architecture", "Progression motion"]
CORRECT = dict(zip(LEFT, ["Deployment and operating model", "Progression motion", "Capability or architecture", "User or product surface", "Codex"]))
card = lambda t: f'<div class="card" style="height:90px;margin:10px 0;background:#ddd;display:flex;align-items:center;justify-content:center"><span>{t}</span></div>'


async def run_drag_case(page, name, sortables) -> int:
    html = DRAG_PAGE % {"left": "".join(map(card, LEFT)), "right": "".join(map(card, RIGHT_SCRAMBLED)),
                        "eqs": "".join('<div style="height:90px;margin:10px 0;display:flex;align-items:center">=</div>' for _ in LEFT),
                        "sortables": sortables}
    await page.set_content(html)
    ctl = BrowserController()
    ctl.page = page
    dom = await ctl.extract_interactive_dom()
    ex = dom["matchingExercise"]
    ok_detect = bool(ex) and "matched" not in ex["status"] and len(ex["scenarioCards"]) >= 4
    cards = await ctl.read_matching_cards()
    ok_detect = ok_detect and bool(cards) and len(cards[0]) == len(cards[1]) == 5   # includes the 5-letter "Codex" card
    solved = await ctl.solve_by_reorder([(l, r) for l, r in CORRECT.items()])
    col = lambda c: f"[...document.querySelectorAll('#{c} .card')].map(c => c.innerText.trim())"
    left_now, final = await page.evaluate(col("L")), await page.evaluate(col("R"))
    ok = ok_detect and solved and all(CORRECT[l] == r for l, r in zip(left_now, final))   # every ROW pairs up, whichever side moved
    print(f"[drag {name}] " + ("PASS" if ok else f"FAIL: detected={ok_detect} solved={solved} final={final}"))
    return 0 if ok else 1


ASSESS_PAGE = """<body style="margin:0;background:#000;color:#fff;font:16px sans-serif">
<div style="position:fixed;left:0;top:0;width:50px;height:100vh;background:#fff"><button style="margin-top:120px" onclick="window.sb++">home</button></div>
<div style="margin-left:300px;padding:20px;width:900px">
  <div style="display:flex;align-items:center;gap:10px;height:30px;margin-left:200px">
    <button id="prev" aria-label="back" onclick="go(-1)" style="width:22px;height:22px">&lsaquo;</button>
    <span id="dots" style="display:flex;gap:8px;align-items:center"></span>
    <button id="nxt" aria-label="forward" onclick="go(1)" style="width:22px;height:22px">&rsaquo;</button>
  </div>
  <h3 id="q"></h3><div id="opts"></div><button id="submit" disabled>Submit</button> or press Enter
</div>
<button id="complete" aria-disabled="true" style="position:fixed;right:10px;bottom:10px;opacity:.5" onclick="clickComplete()">Complete the assessment</button>
<script>
const N=5; let i=0; const done={}; window.sb=0; window.fin=0; window.early=0; window.submitted=[];
function render(){
  document.getElementById('dots').innerHTML=[...Array(N).keys()].map(k=>`<i style="display:block;border-radius:50%;background:#fff;width:${k==i?9:5}px;height:${k==i?9:5}px"></i>`).join('');
  document.getElementById('prev').style.opacity=i==0?.4:1;
  document.getElementById('q').textContent='Assessment item '+(i+1)+': which statement is the best fit here?';
  document.getElementById('opts').innerHTML=['Alpha statement number '+(i+1),'Beta statement number '+(i+1),'Gamma statement number '+(i+1)].map(t=>
    `<label style="display:block;padding:18px;margin:6px 0;background:#222"><input type="radio" name="o" ${done[i]?'disabled':''} ${done[i]=='Alpha'?'checked':''} onchange="document.getElementById('submit').disabled=false"> ${t}</label>`).join('');
  document.getElementById('submit').disabled=true;
  const all=Object.keys(done).length==N; const c=document.getElementById('complete');
  c.setAttribute('aria-disabled', all?'false':'true'); c.style.opacity=all?1:.5;
}
function go(d){i=Math.max(0,Math.min(N-1,i+d));render();}
document.getElementById('submit').onclick=()=>{const r=document.querySelector('input:checked');if(!r)return;done[i]='Alpha';submitted.push(i);render();};
function clickComplete(){ if(document.getElementById('complete').getAttribute('aria-disabled')==='true') early++; else fin++; }
render();
</script></body>"""


async def run_assessment_case(page) -> int:
    await page.set_content(ASSESS_PAGE)
    ctl = BrowserController()
    ctl.page = page
    runner = CourseRunner(controller=ctl, analyzer=None)
    dom = await ctl.extract_interactive_dom()
    a = dom.get("assessment") or {}
    probs = []
    if (a.get("total"), a.get("current")) != (5, 0): probs.append(f"dots total/current wrong: {a.get('total')}/{a.get('current')}")
    if not (a.get("completeButton") or {}).get("disabled"): probs.append("Complete button should read as disabled")
    if not a.get("nextArrow"): probs.append("next arrow not found")

    def analysis(action, **kw):
        return AnalysisResponse.model_validate({"detected_task_type": "SINGLE_SELECT_MCQ", "page_state": "QUIZ_QUESTION",
                                                "observations": "x", "action": {"action_type": action, "explanation": "x", **kw}, **({"quiz": kw.pop("quiz")} if "quiz" in kw else {})})

    runner.current_dom_data = dom
    early = analysis("CLICK_TEXT", target_text="Complete the assessment")
    if not await runner._block_premature_complete(early, dom): probs.append("premature Complete (by text) not blocked")
    by_xy = analysis("CLICK_COORDINATES", coordinates={"x": a["completeButton"]["x"], "y": a["completeButton"]["y"]})
    if not await runner._block_premature_complete(by_xy, dom): probs.append("premature Complete (by coordinates) not blocked")

    for _ in range(40):                       # simulated agent loop: fake model always picks option 1
        if await page.evaluate("window.fin"):
            break
        dom = await ctl.extract_interactive_dom()
        runner.current_dom_data = dom
        if await runner._assessment_turn(dom):
            continue
        opt = dom["quizData"]["options"][0]["text"]
        step = AnalysisResponse.model_validate({"detected_task_type": "SINGLE_SELECT_MCQ", "page_state": "QUIZ_QUESTION", "observations": "x",
                                                "quiz": {"question_text": "Assessment item", "selected_option_texts": [opt], "explanation": "x"},
                                                "action": {"action_type": "SELECT_QUIZ_OPTION", "explanation": "x"}})
        await runner.execute_action(step)
    fin, early_n, sub, sb = [await page.evaluate(f"window.{k}") for k in ("fin", "early", "submitted", "sb")]
    if fin != 1: probs.append(f"Complete clicked {fin} times (want 1)")
    if early_n: probs.append(f"Complete pressed {early_n}x BEFORE all questions were answered")
    if sorted(sub) != [0, 1, 2, 3, 4]: probs.append(f"questions submitted: {sub}")
    if sb: probs.append("icon rail was clicked")
    print("[assessment] " + ("PASS" if not probs else "FAIL: " + "; ".join(probs)))
    return len(probs)


QUIZ_PAGE = """<body style="margin:0;background:#000;color:#fff;font:20px sans-serif"><div style="padding:8px">
<div id=q style="margin:20px 0"></div><div style="font-weight:bold;margin-bottom:20px">Pick the best option</div>
<div id=dots style="display:flex;gap:8px;margin:10px 0"></div><div id=rows></div><div id=exp></div>
<button id=complete aria-disabled="true" style="position:fixed;right:10px;bottom:10px;opacity:.5;display:none" onclick="done()">Complete the assessment</button>
<div style="margin-top:30px"><button id=sub disabled>Submit</button> <span>or press Enter</span><button id=nxt style="display:none"></button></div></div>
<script>
const N=4; let i=0, sel=-1, phase='q'; window.fin=0; window.submitted=[]; window.early=0;
function render(){
  q.textContent='Question text number '+(i+1)+' about refunds?';
  if(typeof DOTS!='undefined'){ dots.innerHTML=[...Array(N).keys()].map(k=>`<i style="display:block;border-radius:50%;background:#fff;width:${k==i?9:5}px;height:${k==i?9:5}px"></i>`).join(''); complete.style.display='';
    const all=submitted.length==N&&phase=='exp'; complete.setAttribute('aria-disabled',all?'false':'true'); complete.style.opacity=all?1:.5; }
  rows.innerHTML=[0,1,2,3].map(k=>`<div style="margin:8px 0"><div class=o data-k=${k} style="background:#1c1c1c;height:90px;display:flex;align-items:center;padding-left:20px"><i style="width:22px;height:22px;border:2px solid #444;border-radius:50%;margin-right:30px;background:${k==sel?'#fff':''}"></i><span>Option ${'ABCD'[k]} of question ${i+1}, long enough to be a real answer</span></div></div>`).join('');
  if(phase=='q') document.querySelectorAll('.o').forEach(o=>o.onclick=()=>{sel=+o.dataset.k;render();setTimeout(()=>{sub.disabled=false},700);});     // Submit lights up late, like the real page
  sub.style.display=phase=='q'?'':'none'; nxt.style.display=phase=='q'?'none':'';
  nxt.textContent=i==N-1?'Complete the test':'Next →'; if(typeof DOTS!='undefined'&&i==N-1) nxt.style.display='none'; exp.innerHTML=phase=='q'?'':'<p>Correct. The approval boundary is what limits the damage of a bad tool call.</p>';
}
sub.onclick=()=>{ if(sel<0) return; submitted.push(i); phase='exp'; render(); };
function done(){ if(complete.getAttribute('aria-disabled')==='true') early++; else fin++; }
nxt.onclick=()=>{ if(i==N-1){fin++;return;} i++; sel=-1; phase='q'; sub.disabled=true; render(); };
render();
</script></body>"""


async def run_quiz_case(page, dotted=False) -> int:
    """Custom <div> options (no inputs / roles / classes): every question is answered + submitted, explanation read, Next pressed, test completed."""
    page = await (await page.context.browser.new_context(viewport={"width": 1280, "height": 800})).new_page()   # fresh window: earlier fixtures leave script globals behind
    await page.set_content(QUIZ_PAGE.replace("const N=4;", "const DOTS=1; const N=4;") if dotted else QUIZ_PAGE)
    ctl = BrowserController(); ctl.page = page
    runner = CourseRunner(controller=ctl, analyzer=None)
    async def solver_quiz(q, opts, required, practice=False): return {"indices": [1], "confidence": .9, "source": "test", "key": "k"}
    runner.solver.answer = solver_quiz
    for _ in range(30):
        if await page.evaluate("window.fin"):
            break
        dom = await ctl.extract_interactive_dom()
        runner.current_dom_data = dom
        if not (await runner._quiz_turn(dom) or await runner._assessment_turn(dom)):
            break
    sub, fin, early = [await page.evaluate(f"window.{k}") for k in ("submitted", "fin", "early")]
    probs = []
    if sub != [0, 1, 2, 3]: probs.append(f"questions submitted: {sub} (want all 4, in order)")
    if fin != 1: probs.append(f"'Complete' clicked {fin} times (want 1)")
    if early: probs.append(f"'Complete' pressed {early}x before all questions were answered")
    print("[custom-div quiz%s] " % (" + dots" if dotted else "") + ("PASS" if not probs else "FAIL: " + "; ".join(probs)))
    return len(probs)


async def run_solver_case(page) -> int:
    """The dedicated solver's answer must beat a wrong page-analysis answer (quiz AND drag-matching)."""
    probs = []
    # --- quiz: page-analysis model says Gamma, solver says Alpha
    await page.set_content("""<body><div>Question 02/20</div><p>Which statement best describes a sensible first step here?</p>
      <label><input type="radio" name="o"> Alpha option text here</label><br><label><input type="radio" name="o"> Gamma option text here</label></body>""")
    ctl = BrowserController(); ctl.page = page
    runner = CourseRunner(controller=ctl, analyzer=None)
    async def solver_quiz(q, opts, required, practice=False): return {"indices": [opts.index("Alpha option text here")], "confidence": .9, "source": "test", "key": "k"}
    runner.solver.answer = solver_quiz
    runner.current_dom_data = await ctl.extract_interactive_dom()
    await runner.execute_action(AnalysisResponse.model_validate({
        "detected_task_type": "SINGLE_SELECT_MCQ", "page_state": "QUIZ_QUESTION", "observations": "q",
        "quiz": {"question_text": "first step", "selected_option_texts": ["Gamma option text here"], "explanation": "x"},
        "action": {"action_type": "SELECT_QUIZ_OPTION", "explanation": "x"}}))
    if await page.evaluate("[...document.querySelectorAll('input')].map(i => i.checked)") != [True, False]:
        probs.append("quiz: solver answer did not override the page-analysis answer")

    # --- matching: model gives swapped (wrong) pairs, solver gives the right permutation
    html = DRAG_PAGE % {"left": "".join(map(card, LEFT)), "right": "".join(map(card, RIGHT_SCRAMBLED)),
                        "eqs": "".join('<div style="height:90px;margin:10px 0;display:flex;align-items:center">=</div>' for _ in LEFT),
                        "sortables": "sortable('R')"}
    await page.set_content(html)
    ctl = BrowserController(); ctl.page = page
    runner = CourseRunner(controller=ctl, analyzer=None)
    async def solver_match(left, right): return [right.index(CORRECT[l]) for l in left]
    runner.solver.match = solver_match
    runner.current_dom_data = await ctl.extract_interactive_dom()
    wrong = [{"scenario_text": LEFT[i], "match_text": RIGHT_SCRAMBLED[i]} for i in range(5)]   # deliberately wrong pairing
    await runner.execute_action(AnalysisResponse.model_validate({
        "detected_task_type": "MATCHING_PAIRS_OR_PUZZLE", "page_state": "INTERACTIVE_EXERCISE", "observations": "m",
        "matching_pairs": wrong, "action": {"action_type": "SOLVE_MATCHING_PAIRS", "explanation": "x"}}))
    col = lambda c: f"[...document.querySelectorAll('#{c} .card')].map(c => c.innerText.trim())"
    left_now, right_now = await page.evaluate(col("L")), await page.evaluate(col("R"))
    if not all(CORRECT[l] == r for l, r in zip(left_now, right_now)):
        probs.append(f"matching: rows not paired by solver result: {list(zip(left_now, right_now))}")
    print("[solver integration] " + ("PASS" if not probs else "FAIL: " + "; ".join(probs)))
    return len(probs)


async def main() -> int:
    async with async_playwright() as p:
        try:  # same browser the app uses; falls back to bundled Chromium
            browser = await p.chromium.launch(channel="chrome", headless=True)
        except Exception:
            browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        bad = sum([await run_variant(page, n, h) for n, h in VARIANTS.items()]) + await run_hint_case(page)
        bad += await run_drag_case(page, 'right column draggable', "sortable('R')")
        bad += await run_drag_case(page, 'only left column draggable', "sortable('L')")
        bad += await run_assessment_case(page)
        bad += await run_quiz_case(page)
        bad += await run_quiz_case(page, dotted=True)
        bad += await run_solver_case(page)
        await browser.close()
    return bad


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
