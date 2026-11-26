import asyncio
import base64
import re
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from rich.console import Console

from src.config import settings

console = Console()

# Shared JS helpers, injected into every page script via the /*H*/ placeholder.
# markSidebar() tags the side navigation / course outline with data-agent-sidebar so that
# NOTHING in it is ever read, matched or clicked. Detection is semantic (aside/nav/*sidebar*)
# OR layout based (a tall, narrow rail glued to the left/right edge), so it adapts to markup changes.
HELPERS_JS = r"""
const isRendered = (el) => {
    if (!el) return false;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
};
const clean = (el) => (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ');
const norm = (s) => (s || '').toLowerCase().replace(/[^a-z0-9\s]/g, ' ').replace(/\s+/g, ' ').trim();
const inSidebar = (el) => !!(el && el.closest && el.closest('[data-agent-sidebar]'));
const markSidebar = () => {
    document.querySelectorAll('[data-agent-sidebar]').forEach(e => e.removeAttribute('data-agent-sidebar'));
    const iw = window.innerWidth, ih = window.innerHeight;
    const strong = 'aside, [role="complementary"], [class*="sidebar" i], [class*="side-bar" i], [class*="sidenav" i]';
    const navs = 'nav, [role="navigation"]';
    const found = [];
    document.querySelectorAll(strong + ',' + navs + ', div, section, ul, ol').forEach(el => {
        const r = el.getBoundingClientRect();
        if (r.width < 30 || r.width > iw * 0.5 || r.height < 20) return;
        // a layout-only rail must look like NAVIGATION: narrow, edge-glued, tall and holding >= 2 links/buttons/icons (a column of plain cards is content)
        const rail = r.height >= ih * 0.6 && r.width <= iw * 0.28 && (r.left <= 8 || r.right >= iw - 8)
            && el.querySelectorAll('a, button, [role="button"], [role="link"], [role="menuitem"], svg').length >= 2;
        const semantic = el.matches(strong) || (el.matches(navs) && r.height >= ih * 0.4);
        if (rail || semantic) found.push(el);
    });
    found.forEach(el => {
        if (!found.some(o => o !== el && o.contains(el))) el.setAttribute('data-agent-sidebar', '1');
    });
};
const findText = (re) => {
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = w.nextNode())) {
        const t = n.nodeValue.trim();
        if (t && re.test(t)) {
            const p = n.parentElement;
            if (p && isRendered(p) && !inSidebar(p)) return p;
        }
    }
    return null;
};
// Answer rows of the quiz on screen: native inputs / ARIA first, then class hints, then pure layout
// (equal-width cards stacked in a column, each with a radio/checkbox-like marker) so custom <div> options are found too.
const optionRows = () => {
    const ok = (el) => isRendered(el) && !inSidebar(el);
    const dedupe = (els) => { const s = new Set(); return els.filter(e => !s.has(e) && s.add(e)); };
    const inputs = Array.from(document.querySelectorAll('input[type="radio"], input[type="checkbox"], [role="radio"], [role="checkbox"]')).filter(ok);
    if (inputs.length) return dedupe(inputs.map(i => i.closest('label, li, [class*="option"], [class*="choice"]') || i.parentElement || i));
    const hinted = Array.from(document.querySelectorAll('label, [class*="option"], [class*="choice"], li')).filter(el => {
        if (!ok(el)) return false;
        const r = el.getBoundingClientRect(), t = clean(el);
        return r.height > 15 && r.height < 200 && r.width > 120 && t.length > 3 && t.length < 350 && !/submit|question|\d+\/\d+/i.test(t);
    });
    const marker = (b) => Array.from(b.querySelectorAll('*')).some(e => {
        const r = e.getBoundingClientRect();
        return !clean(e) && r.width >= 10 && r.width <= 48 && Math.abs(r.width - r.height) <= 4 && isRendered(e);
    });
    const boxes = [];
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = w.nextNode())) {
        let b = n.parentElement;
        if (!b || !n.nodeValue.trim() || !ok(b)) continue;
        const t = clean(b);
        while (b.parentElement && b.parentElement !== document.body && clean(b.parentElement) === t) b = b.parentElement;
        const r = b.getBoundingClientRect();
        if (r.height >= 36 && r.height <= 220 && r.width >= 160 && t.length > 2 && t.length < 350 && !/^(submit|check answer|next|continue)\b|press enter/i.test(t)) boxes.push(b);
    }
    const groups = {};
    dedupe(boxes).forEach(b => {
        const r = b.getBoundingClientRect(), k = Math.round(r.left / 8) + ':' + Math.round(r.width / 16);
        (groups[k] = groups[k] || []).push(b);
    });
    const score = (g) => g.length + 10 * g.filter(marker).length;
    const best = Object.values(groups).filter(g => g.length >= 2 && g.length <= 8).sort((a, b) => score(b) - score(a))[0] || [];
    best.sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
    return best.length > hinted.length ? best : hinted;
};
const scroller = () => {
    let el = document.elementFromPoint(window.innerWidth / 2, window.innerHeight / 2);
    while (el && el !== document.body && el !== document.documentElement) {
        const s = getComputedStyle(el);
        if (/(auto|scroll)/.test(s.overflowY) && el.scrollHeight > el.clientHeight + 10) return el;
        el = el.parentElement;
    }
    return document.scrollingElement || document.documentElement;
};
"""


def _js(src: str) -> str:
    return src.replace("/*H*/", HELPERS_JS)


EXTRACT_JS = _js(r"""
() => {
    /*H*/
    markSidebar();
    const ok = (el) => isRendered(el) && !inSidebar(el);

    // Page text WITHOUT the sidebar (its outline would otherwise leak 'Lesson N' / '20%' strings into everything)
    let mainText = document.body.innerText || '';
    document.querySelectorAll('[data-agent-sidebar]').forEach(sb => {
        const s = sb.innerText || '';
        if (s && mainText.includes(s)) mainText = mainText.replace(s, '');
    });
    const lines = mainText.split('\n').map(l => l.trim()).filter(Boolean);

    const headings = Array.from(document.querySelectorAll('h1, h2, h3, h4')).filter(ok)
        .map(h => ({ level: h.tagName, text: clean(h) })).filter(h => h.text.length > 0);

    const buttons = Array.from(document.querySelectorAll(
        'button, [role="button"], [role="radio"], [role="tab"], .pill, .chip, [class*="pill"], [class*="chip"], [class*="suggestion"], a, input[type="submit"], input[type="button"]'
    )).filter(ok).map(btn => {
        const rect = btn.getBoundingClientRect();
        return {
            tag: btn.tagName.toLowerCase(),
            text: clean(btn).slice(0, 80),
            ariaLabel: btn.getAttribute('aria-label') || '',
            inViewport: rect.top < window.innerHeight && rect.bottom > 0,
            disabled: btn.disabled || btn.getAttribute('aria-disabled') === 'true' || btn.classList.contains('disabled'),
            x: Math.round(rect.x + rect.width / 2),
            y: Math.round(rect.y + rect.height / 2)
        };
    }).filter(b => b.text.length > 0 || b.ariaLabel.length > 0).slice(0, 45);

    // ---- Locked-progression detection (sidebar excluded: its future lessons carry lock icons) ----
    const lockLine = mainText.match(/[^\n]*(complete the content above|complete the question|locked until|select either side to start)[^\n]*/i);
    const lockIcon = Array.from(document.querySelectorAll('svg[data-icon="lock"], [data-lucide="lock"], .lucide-lock, [aria-label], [class*="lock" i]')).some(el => {
        if (!ok(el)) return false;
        const a = el.getAttribute('aria-label') || '';
        const c = String(el.getAttribute('class') || '');
        return el.matches('svg[data-icon="lock"], [data-lucide="lock"], .lucide-lock')
            || /\block(ed)?\b/i.test(a)
            || /(^|[\s_-])lock(ed)?($|[\s_-])/i.test(c);   // 'is-locked' yes; 'block', 'unlock', 'clock' no
    });
    const mm = mainText.match(/^\s*(\d+\s+of\s+\d+\s+matched)/im);
    const matchStatusText = mm ? mm[1] : (/^\s*Select either side to start\.?\s*$/im.test(mainText) ? 'Select either side to start.' : null);
    const hasIncompleteMatch = matchStatusText && !/^(\d+)\s+of\s+\1\s+matched/i.test(matchStatusText);

    const isProgressionLocked = Boolean(lockLine) || lockIcon || Boolean(hasIncompleteMatch);
    let lockedText = '';
    if (lockLine) lockedText = lockLine[0].trim().slice(0, 100);
    else if (isProgressionLocked) lockedText = 'Progression locked by exercise above (' + (matchStatusText || 'lock icon present') + ')';

    // ---- Primary progression button: lesson pill / Continue / Next. Last interactive match wins (bottom of page). ----
    const lessonRe = /(^|\b)(lesson\s+\d+(\.\d+)?|module\s+\d+|next\s+lesson|complete\s*(&|\+|and)?\s*continue)/i;
    const genericRe = /^(continue|next|finish|done)$/i;
    const pmatches = [];
    document.querySelectorAll('button, [role="button"], a, [class*="button"], [class*="btn"], [class*="pill"], input[type="button"], input[type="submit"], div, span').forEach(el => {
        const txt = (el.textContent || '').trim().replace(/\s+/g, ' ');
        if (!txt || txt.length > 120) return;
        const generic = genericRe.test(txt) && el.matches('button, a, [role="button"], input');
        if (!(lessonRe.test(txt) || generic)) return;
        if (Array.from(el.children).some(c => lessonRe.test((c.textContent || '').trim()))) return;   // prefer leaf
        if (!ok(el)) return;
        pmatches.push(el);
    });
    const interactiveHits = pmatches.filter(e => e.closest('button, a, [role="button"], input'));
    const pel = (interactiveHits.length ? interactiveHits : pmatches).pop();
    let primaryProgression = null;
    if (pel) {
        const txt = clean(pel);
        const rect = pel.getBoundingClientRect();
        const targetEl = pel.closest('button, a, [role="button"]') || pel;
        const locked = isProgressionLocked || /complete the content above/i.test(txt) || Boolean(targetEl.querySelector('svg[data-icon="lock"]'));
        primaryProgression = {
            tag: targetEl.tagName.toLowerCase(),
            text: txt.slice(0, 100),
            inViewport: rect.top < window.innerHeight && rect.bottom > 0,
            disabled: targetEl.disabled || targetEl.getAttribute('aria-disabled') === 'true' || targetEl.classList.contains('disabled') || locked,
            isLocked: locked,
            x: Math.round(rect.x + rect.width / 2),
            y: Math.round(rect.y + rect.height / 2)
        };
    }

    const inputs = Array.from(document.querySelectorAll('input, textarea, select')).filter(ok).map(inp => {
        const rect = inp.getBoundingClientRect();
        return {
            tag: inp.tagName.toLowerCase(), type: inp.type || 'text', placeholder: inp.placeholder || '',
            value: (inp.value || '').slice(0, 50), ariaLabel: inp.getAttribute('aria-label') || '', disabled: inp.disabled,
            x: Math.round(rect.x + rect.width / 2), y: Math.round(rect.y + rect.height / 2)
        };
    }).slice(0, 20);

    // ---- Matching exercise (card scan only when a matching marker is present: keeps normal steps fast) ----
    let matchingExercise = null;
    const hasScenarioCol = lines.includes('Scenario');
    const hasNextStepCol = lines.includes('Best next step');
    const hasMatchingPrompt = /select the most appropriate way|for each task|match each|match the following|match the pairs/i.test(mainText);
    if (matchStatusText || (hasScenarioCol && hasNextStepCol) || hasMatchingPrompt) {
        const leaf = (el) => !Array.from(el.children).some(c => clean(c).length > 15);
        const box = new Set();
        document.querySelectorAll('*').forEach(el => {
            if (!ok(el)) return;
            const t = clean(el);
            if (t.length <= 15 || !leaf(el) || /submit|question\s*\d+|\d+\/\d+|select the most|scenario|best next step|matched|complete the (content|question)|^match (each|the)|^or press/i.test(t)) return;
            let c = el;
            while (c.parentElement && clean(c.parentElement) === t && c.parentElement.getBoundingClientRect().width <= window.innerWidth * 0.55) c = c.parentElement;
            const r = c.getBoundingClientRect();
            if (r.width > 120 && r.height > 25 && r.height < 250) box.add(c);
        });
        const cards = Array.from(box);
        let splitX = window.innerWidth / 2;
        const h1 = findText(/^Scenario$/), h2 = findText(/^Best next step$/);
        if (h1 && h2) {
            const r1 = h1.getBoundingClientRect(), r2 = h2.getBoundingClientRect();
            splitX = (r1.x + r1.width + r2.x) / 2;
        }
        const leftCards = [], rightCards = [];
        cards.forEach(el => {
            const r = el.getBoundingClientRect();
            const c = { text: clean(el), x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) };
            (r.x + r.width / 2 < splitX ? leftCards : rightCards).push(c);
        });
        matchingExercise = {
            status: matchStatusText || (leftCards.length >= 2 && rightCards.length >= 2 ? `${leftCards.length} pairs exercise` : 'in_progress'),
            scenarioCards: leftCards,
            nextStepCards: rightCards
        };
    }

    // ---- Quiz (single / multi select) ----
    let quizData = null;
    const questionNumberMatch = lines.find(l => /^(\d+\/\d+|question\s*\d+)/i.test(l)) || '';
    const submitBtn = Array.from(document.querySelectorAll('button, input[type="submit"], [role="button"]')).filter(ok)
        .find(el => /^(submit|check answer)$/i.test((el.innerText || el.value || '').trim()));
    let isSubmitEnabled = false;
    if (submitBtn) {
        const s = getComputedStyle(submitBtn);
        isSubmitEnabled = !(submitBtn.disabled || submitBtn.hasAttribute('disabled') || submitBtn.getAttribute('aria-disabled') === 'true'
            || s.pointerEvents === 'none' || parseFloat(s.opacity || '1') < 0.6 || submitBtn.classList.contains('disabled'));
    }
    const rows = matchingExercise ? [] : optionRows();
    if ((submitBtn || (questionNumberMatch && rows.length)) && !matchingExercise) {
        const optionElements = rows;
        const structuredOptions = optionElements.map(el => {
            const inp = el.querySelector('input') || el.parentElement?.querySelector('input');
            const checked = (inp && inp.checked) || el.getAttribute('aria-checked') === 'true' || el.classList.contains('selected') || el.classList.contains('checked');
            const isCheckbox = (inp && inp.type === 'checkbox') || el.getAttribute('role') === 'checkbox' || el.classList.contains('checkbox');
            return { text: clean(el), type: isCheckbox ? 'checkbox' : 'radio', isChecked: Boolean(checked) };
        }).slice(0, 8);

        let questionPrompt = '';
        if (optionElements.length > 0) {
            const top = optionElements[0].getBoundingClientRect().top;
            const above = Array.from(document.querySelectorAll('h1, h2, h3, h4, p, legend, [role="heading"], div')).filter(ok).filter(el => {
                const r = el.getBoundingClientRect(), t = clean(el);
                return r.bottom <= top + 10 && r.bottom > top - 400 && t.length > 15 && t.length < 500 && !/submit/i.test(t);
            }).map(clean);
            questionPrompt = [...new Set(above.filter(t => !above.some(o => o !== t && o.includes(t))))].join(' ');   // drop nested duplicates
        }
        if (!questionPrompt) {
            questionPrompt = lines.find(t => t.length > 20 && t.length < 400 && !t.includes('Question') && !t.includes('SUBMIT')) || '';
        }

        let requiredSelectionCount = 1;
        const multiMatch = questionPrompt.match(/select\s+(two|three|four|five|\d+)/i) || mainText.match(/select\s+(two|three|four|five|\d+)/i);
        if (multiMatch) {
            const numMap = { two: 2, three: 3, four: 4, five: 5 };
            requiredSelectionCount = numMap[multiMatch[1].toLowerCase()] || parseInt(multiMatch[1], 10) || 1;
        } else if (structuredOptions.some(o => o.type === 'checkbox')) {
            requiredSelectionCount = 2;
        }
        quizData = {
            indicator: questionNumberMatch, questionPrompt: questionPrompt,
            isMultiSelect: requiredSelectionCount > 1, requiredCount: requiredSelectionCount,
            hasSubmit: Boolean(submitBtn), isSubmitEnabled: isSubmitEnabled, options: structuredOptions
        };
    }


    // ---- Assessment: a row of progress dots (one per question) + a 'Complete the assessment' button ----
    let assessment = null;
    {
        const dotEls = Array.from(document.querySelectorAll('button, [role="button"], [role="tab"], li, span, div, a, i')).filter(el => {
            const r = el.getBoundingClientRect();
            return r.width >= 3 && r.width <= 18 && r.height >= 3 && r.height <= 18 && Math.abs(r.width - r.height) <= 4
                && el.children.length === 0 && !(el.textContent || '').trim() && ok(el);
        });
        const rowsOf = [];
        dotEls.forEach(el => {
            const y = el.getBoundingClientRect().y;
            const row = rowsOf.find(g => Math.abs(g.y - y) <= 4);
            if (row) row.items.push(el); else rowsOf.push({ y, items: [el] });
        });
        const best = rowsOf.filter(g => g.items.length >= 5).sort((a, b) => b.items.length - a.items.length)[0];
        const dots = best ? best.items.sort((a, b) => a.getBoundingClientRect().x - b.getBoundingClientRect().x) : [];
        const center = (el) => { const r = el.getBoundingClientRect(); return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) }; };
        const isOff = (el) => {
            const t = el.closest('button, [role="button"], a') || el;
            const s = getComputedStyle(t);
            return Boolean(t.disabled || t.getAttribute('aria-disabled') === 'true' || t.hasAttribute('data-disabled') || /disabled/i.test(String(t.getAttribute('class') || ''))
                || s.pointerEvents === 'none' || parseFloat(s.opacity || '1') < 0.8);
        };

        // which dot is the current question? aria/class flag, else the one that is uniquely bigger / more opaque / differently coloured
        let current = null;
        if (dots.length) {
            const flagged = dots.findIndex(d => {
                const a = d.closest('[aria-current]');
                return (a && a.getAttribute('aria-current') !== 'false') || d.getAttribute('aria-selected') === 'true'
                    || /(^|[\s_-])(active|current|selected)($|[\s_-])/i.test(String(d.getAttribute('class') || ''));
            });
            const uniqueBy = (fn) => {
                const vals = dots.map(fn);
                const counts = {};
                vals.forEach(v => { counts[v] = (counts[v] || 0) + 1; });
                const odd = vals.filter(v => counts[v] === 1);
                return odd.length === 1 && Object.keys(counts).length > 1 ? vals.indexOf(odd[0]) : -1;
            };
            const bySize = uniqueBy(d => Math.round(d.getBoundingClientRect().width));
            const byOpacity = uniqueBy(d => getComputedStyle(d).opacity);
            const byColor = uniqueBy(d => getComputedStyle(d).backgroundColor);
            current = flagged >= 0 ? flagged : bySize >= 0 ? bySize : byOpacity >= 0 ? byOpacity : byColor >= 0 ? byColor : null;
        }

        const arrowNear = (side) => {
            if (!dots.length) return null;
            const ref = center(side === 'next' ? dots[dots.length - 1] : dots[0]);
            const dotSet = new Set(dots);
            const cands = Array.from(document.querySelectorAll('button, [role="button"], a, svg, [class*="arrow"], [class*="chevron"]')).filter(el => {
                if (dotSet.has(el) || !ok(el)) return false;
                const r = el.getBoundingClientRect(), c = center(el);
                const dx = side === 'next' ? c.x - ref.x : ref.x - c.x;
                return r.width <= 60 && r.height <= 60 && Math.abs(c.y - ref.y) <= 30 && dx > 4 && dx < 140;
            }).sort((a, b) => Math.abs(center(a).x - ref.x) - Math.abs(center(b).x - ref.x));
            const el = cands[0];
            return el ? Object.assign(center(el), { disabled: isOff(el) }) : null;
        };

        const completeEl = Array.from(document.querySelectorAll('button, [role="button"], a, div, span')).filter(el => {
            const t = (el.textContent || '').trim().replace(/\s+/g, ' ');
            return t.length < 60 && /^complete (the )?(assessment|quiz|test|exam)$/i.test(t) && ok(el);
        }).pop();   // last = deepest element of the last matching branch

        if (dots.length || completeEl) {
            assessment = {
                total: dots.length,
                current: current,
                dots: dots.map(center),
                nextArrow: arrowNear('next'),
                prevArrow: arrowNear('prev'),
                completeButton: completeEl ? Object.assign(center(completeEl), { text: clean(completeEl), disabled: isOff(completeEl) }) : null
            };
        }
    }

    return {
        url: window.location.href,
        title: document.title,
        assessment: assessment,
        headings: headings.slice(0, 8),
        buttons: buttons,
        inputs: inputs,
        matchingExercise: matchingExercise,
        quizData: quizData,
        isProgressionLocked: isProgressionLocked,
        lockedStatusMessage: lockedText,
        primaryProgressionButton: primaryProgression,
        bodyTextPreview: mainText.slice(0, 3000).replace(/\s+/g, ' '),
        sidebarIgnored: document.querySelectorAll('[data-agent-sidebar]').length,
        viewport: {
            width: window.innerWidth, height: window.innerHeight, devicePixelRatio: window.devicePixelRatio || 1,
            scrollX: window.scrollX, scrollY: window.scrollY,
            scrollHeight: Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)
        }
    };
}
""")

# Fuzzy text -> element finder (strategy 2 of click_by_text). Returns coordinates only; the click itself is native.
FIND_TEXT_JS = _js(r"""
({ target, requireEnabled }) => {
    /*H*/
    markSidebar();
    const nt = norm(target);
    if (!nt) return { success: false };
    const words = nt.split(' ').filter(w => w.length >= 2);
    const search = (doc) => {
        const matches = [];
        for (const el of doc.querySelectorAll('*')) {
            if (inSidebar(el) || el.querySelector('[data-agent-sidebar]')) continue;   // sidebar, or a wrapper around it
            const rect = el.getBoundingClientRect();
            if (rect.width <= 20 || rect.height <= 10) continue;
            const s = getComputedStyle(el);
            if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') continue;
            const txt = norm(el.innerText || el.textContent || el.getAttribute('aria-label') || el.value || '');
            if (!txt) continue;
            const disabled = el.disabled === true || el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true'
                || el.classList.contains('disabled') || s.pointerEvents === 'none' || parseFloat(s.opacity || '1') < 0.6
                || txt.includes('complete the content above') || txt.includes('complete the question') || Boolean(el.closest('[disabled], [aria-disabled="true"], .disabled'));
            if (requireEnabled && disabled) continue;

            let score = 0;
            if (txt === nt) score = 100;
            else if (txt.includes(nt) || (txt.length >= 4 && nt.includes(txt))) score = 85;
            else if (nt.startsWith('lesson ') && txt.includes(nt.slice(0, 10))) score = 90;
            else {
                const hits = words.filter(w => txt.includes(w)).length;
                if (words.length > 0 && hits >= Math.min(words.length, 3)) score = (hits / words.length) * 75;
            }
            if (score < 50) continue;
            const hasMatchingChild = Array.from(el.children).some(c => {
                const ct = norm(c.innerText || c.textContent || '');
                return ct && (ct.includes(nt) || nt.includes(ct));
            });
            const interactive = el.matches('button, a, [role="button"], [class*="card"], [class*="pill"], [class*="chip"]');
            if (hasMatchingChild && !interactive) continue;   // a plain wrapper only matches through its child: the child is the target
            matches.push({ el, score, hasMatchingChild, area: rect.width * rect.height, interactive });
        }
        if (!matches.length) return null;
        matches.sort((a, b) =>
            (a.hasMatchingChild !== b.hasMatchingChild) ? (a.hasMatchingChild ? 1 : -1)
            : (b.score !== a.score) ? b.score - a.score
            : (a.interactive !== b.interactive) ? (a.interactive ? -1 : 1)
            : a.area - b.area);
        const target_el = matches[0].el.closest('button, a, [role="button"]') || matches[0].el;
        target_el.scrollIntoView({ behavior: 'instant', block: 'center', inline: 'center' });
        const r = target_el.getBoundingClientRect();
        return { success: true, x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) };
    };
    let res = search(document);
    if (res) return res;
    for (const f of document.querySelectorAll('iframe')) {
        try { if (f.contentDocument && (res = search(f.contentDocument))) return res; } catch (e) {}
    }
    return { success: false };
}
""")

# Matching-card scorer, shared by get_matching_card_info (no scroll) and click_matching_card (scroll first).
FIND_CARD_JS = _js(r"""
({ target, scroll }) => {
    /*H*/
    markSidebar();
    const t = norm(target);
    const words = t.split(' ').filter(w => w.length >= 3);
    let best = null, bestScore = 0, bestArea = Infinity;
    for (const el of document.querySelectorAll('*')) {
        const r = el.getBoundingClientRect();
        if (r.width <= 100 || r.height <= 25) continue;
        if (!isRendered(el) || inSidebar(el) || el.querySelector('[data-agent-sidebar]')) continue;
        const txt = norm(el.innerText || el.textContent || '');
        if (txt.length < 5 || txt === 'scenario' || txt === 'best next step' || txt.includes('matched') || txt.includes('complete the content')) continue;
        let score = 0;
        if (txt === t) score = 100;
        else if ((txt.includes(t) && txt.length <= t.length * 2) || t.includes(txt)) score = 90;   // not a container of many cards
        else if (words.length) score = (words.filter(w => txt.includes(w)).length / words.length) * 80;
        const area = r.width * r.height;
        if (score > bestScore || (score === bestScore && score > 0 && area < bestArea)) { best = el; bestScore = score; bestArea = area; }
    }
    if (!best || bestScore < 25) return null;
    if (scroll) best.scrollIntoView({ behavior: 'instant', block: 'center', inline: 'center' });
    const r = best.getBoundingClientRect();
    const s = getComputedStyle(best);
    const active = s.backgroundColor === 'rgb(0, 0, 0)' || s.color === 'rgb(255, 255, 255)'
        || best.classList.contains('selected') || best.classList.contains('active') || best.getAttribute('aria-selected') === 'true';
    return {
        score: bestScore, alreadyActive: active,
        x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2),
        box: { x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height) }
    };
}
""")

# Reads every matching card (left/right column split at the widest horizontal gap) plus the '=' connectors.
GET_CARDS_JS = _js(r"""
() => {
    /*H*/
    markSidebar();
    const iw = window.innerWidth;
    // text leaves -> climb to the full card box (same text, still column-sized) -> only then size-filter the BOX
    const set = new Set();
    document.querySelectorAll('*').forEach(el => {
        if (!isRendered(el) || inSidebar(el)) return;
        const t = clean(el);
        if (t.length < 3 || /^(submit|match (each|the)|complete the|or press)/i.test(t) || /\d+\/\d+|question\s*\d+/i.test(t)) return;
        if (Array.from(el.children).some(c => clean(c).length >= 3)) return;
        let c = el;
        while (c.parentElement && clean(c.parentElement) === t && c.parentElement.getBoundingClientRect().width <= iw * 0.55) c = c.parentElement;
        const r = c.getBoundingClientRect();
        if (r.width > 100 && r.height > 25 && r.height < 250) set.add(c);
    });
    const cards = Array.from(set).map(el => {
        const r = el.getBoundingClientRect();
        return { text: clean(el), cx: Math.round(r.x + r.width / 2), cy: Math.round(r.y + r.height / 2), h: Math.round(r.height) };
    });
    const eqs = Array.from(document.querySelectorAll('*')).filter(el => el.children.length === 0 && (el.textContent || '').trim() === '=' && isRendered(el) && !inSidebar(el))
        .map(el => { const r = el.getBoundingClientRect(); return { cx: Math.round(r.x + r.width / 2), cy: Math.round(r.y + r.height / 2) }; });

    // column split: the '=' knobs sit between the columns; otherwise the widest horizontal gap
    let split = iw / 2;
    if (eqs.length >= 2) {
        split = eqs.map(e => e.cx).sort((a, b) => a - b)[Math.floor(eqs.length / 2)];
    } else {
        const xs = cards.map(c => c.cx).sort((a, b) => a - b);
        let gap = 0;
        for (let i = 1; i < xs.length; i++) if (xs[i] - xs[i - 1] > gap) { gap = xs[i] - xs[i - 1]; split = (xs[i] + xs[i - 1]) / 2; }
    }
    // keep only cards that sit on an exercise ROW (aligned with a '=' knob, or with a card in the other column)
    let kept = eqs.length >= 2
        ? cards.filter(c => eqs.some(e => Math.abs(e.cy - c.cy) < Math.max(40, c.h * 0.6)))
        : cards.filter(c => cards.some(o => (o.cx < split) !== (c.cx < split) && Math.abs(o.cy - c.cy) < Math.max(o.h, c.h) * 0.6));
    if (kept.length < 4) kept = cards;
    kept.forEach(c => { c.col = c.cx < split ? 'L' : 'R'; });
    return { cards: kept, total: cards.length, eqs };
}
""")

# Finds the MCQ option row, scrolls to it and TAGS it (data-agent-target). No clicking here: one native click, then verify.
FIND_OPTION_JS = _js(r"""
({ target, targetIndex }) => {
    /*H*/
    markSidebar();
    document.querySelectorAll('[data-agent-target]').forEach(e => e.removeAttribute('data-agent-target'));
    const tn = norm(target);
    const tw = new Set(tn.split(' ').filter(w => w.length >= 2));

    const rows = optionRows();

    const words = (el) => new Set(norm(clean(el)).split(' ').filter(w => w.length >= 2));
    let chosen = null;
    if (typeof targetIndex === 'number' && targetIndex >= 0 && targetIndex < rows.length) {
        const cw = words(rows[targetIndex]);
        if (tw.size === 0 || [...tw].some(w => cw.has(w))) chosen = rows[targetIndex];
    }
    if (!chosen) {
        let bestScore = 0;
        for (const row of rows) {
            const rn = norm(clean(row));
            if (!rn) continue;
            if (rn === tn) { chosen = row; break; }
            const rw = words(row);
            const inter = [...tw].filter(w => rw.has(w)).length;
            const union = new Set([...tw, ...rw]).size;
            const j = union ? inter / union : 0;
            if (j > bestScore && j >= 0.25) { bestScore = j; chosen = row; }
        }
    }
    if (!chosen) return { success: false };

    chosen.scrollIntoView({ behavior: 'instant', block: 'center', inline: 'center' });
    const input = chosen.querySelector('input[type="radio"], input[type="checkbox"]') || chosen.parentElement?.querySelector('input[type="radio"], input[type="checkbox"]');
    const checked = (input && input.checked) || chosen.getAttribute('aria-checked') === 'true'
        || /\b(selected|checked|is-selected|is-checked)\b/i.test(chosen.className || '')
        || Boolean(chosen.querySelector('svg[data-icon="check"], [class*="checked"], [aria-checked="true"]'));
    if (checked) return { success: true, alreadySelected: true };

    const indicator = chosen.querySelector('input, [role="checkbox"], [role="radio"], [class*="checkbox"], [class*="radio"], [class*="circle"], [class*="box"], [class*="control"]') || chosen;
    chosen.setAttribute('data-agent-target', '1');
    const r = (isRendered(indicator) ? indicator : chosen).getBoundingClientRect();
    return { success: true, alreadySelected: false, x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) };
}
""")

# After the native click: is the tagged row checked? If its state is detectable and still unchecked, click it ONCE via DOM.
VERIFY_OPTION_JS = r"""
() => {
    const el = document.querySelector('[data-agent-target]');
    if (!el) return { detectable: false, checked: false };
    const input = el.querySelector('input[type="radio"], input[type="checkbox"]') || el.parentElement?.querySelector('input[type="radio"], input[type="checkbox"]');
    const aria = el.getAttribute('aria-checked') ?? el.querySelector('[aria-checked]')?.getAttribute('aria-checked');
    const detectable = Boolean(input) || aria !== null && aria !== undefined;
    const checked = (input && input.checked) || aria === 'true';
    if (detectable && !checked) el.click();
    el.removeAttribute('data-agent-target');
    return { detectable, checked, retried: detectable && !checked };
}
"""

# Single exact-text popup dismisser (cookie banners / 'Maybe later' / modal close). Never touches the sidebar or page content.
POPUP_JS = _js(r"""
() => {
    /*H*/
    markSidebar();
    const scope = 'dialog, [role="dialog"], [role="alertdialog"], [aria-modal="true"], [class*="modal" i], [class*="popup" i], [class*="toast" i]';
    const hit = Array.from(document.querySelectorAll('button, [role="button"], a')).filter(el => isRendered(el) && !inSidebar(el)).find(el => {
        const t = clean(el), a = el.getAttribute('aria-label') || '';
        if (/^(maybe later|not now|no thanks)$/i.test(t)) return true;
        return (/^(dismiss|close|skip)$/i.test(t) || /^close$/i.test(a)) && Boolean(el.closest(scope));
    });
    if (!hit) return null;
    hit.scrollIntoView({ block: 'center' });
    const r = hit.getBoundingClientRect();
    return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2), text: clean(hit) || 'close' };
}
""")

# Locator-match check (one round trip): not in sidebar, and (optionally) really enabled.
CHECK_EL_JS = r"""
(e, requireEnabled) => {
    if (e.closest('[data-agent-sidebar]')) return false;
    if (!requireEnabled) return true;
    const s = getComputedStyle(e);
    const cls = String(e.getAttribute('class') || '');
    return !(e.disabled === true || e.hasAttribute('disabled') || e.getAttribute('aria-disabled') === 'true' || /disabled/i.test(cls)
        || s.pointerEvents === 'none' || parseFloat(s.opacity || '1') < 0.6 || e.closest('[disabled], [aria-disabled="true"], .disabled'));
}
"""


class BrowserController:
    """Manages Playwright browser sessions, interactions, DOM extraction, and screenshots."""

    def __init__(self):
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._drag_side: Optional[str] = None   # which column is draggable in reorder-style matching ('L'/'R'), learned at runtime

    async def start(self) -> Page:
        """Starts browser context either via CDP or persistent context."""
        self.playwright = await async_playwright().start()

        if settings.cdp_url:
            console.print(f"[bold cyan]Connecting to existing Chrome via CDP at {settings.cdp_url}...[/bold cyan]")
            self.browser = await self.playwright.chromium.connect_over_cdp(settings.cdp_url)
            contexts = self.browser.contexts
            if contexts and contexts[0].pages:
                self.context = contexts[0]
                self.page = self.context.pages[0]
            else:
                self.context = await self.browser.new_context()
                self.page = await self.context.new_page()
        else:
            profile_dir = Path(settings.user_data_dir).resolve()
            profile_dir.mkdir(parents=True, exist_ok=True)
            console.print(f"[bold cyan]Launching browser with persistent profile: {profile_dir}...[/bold cyan]")

            launch_kwargs = {
                "user_data_dir": str(profile_dir),
                "headless": settings.headless,
                "slow_mo": settings.slow_mo_ms,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--start-maximized",
                    "--window-position=50,50"
                ],
                "no_viewport": True,
            }

            try:
                self.context = await self.playwright.chromium.launch_persistent_context(channel="chrome", **launch_kwargs)
                console.print("[dim]Using Google Chrome channel.[/dim]")
            except Exception as e:
                console.print(f"[dim]Google Chrome channel unavailable ({e}), using Chromium...[/dim]")
                self.context = await self.playwright.chromium.launch_persistent_context(**launch_kwargs)

            pages = self.context.pages
            self.page = pages[0] if pages else await self.context.new_page()
            await self.page.bring_to_front()

        self.page.set_default_timeout(15000)
        return self.page

    async def navigate_to_course(self, url: Optional[str] = None) -> None:
        """Navigates to the course URL if not already there."""
        target_url = url or settings.course_url
        current_url = self.page.url
        if not current_url or current_url == "about:blank" or "openai.com" not in current_url:
            console.print(f"[bold green]Navigating to {target_url}...[/bold green]")
            await self.page.goto(target_url, wait_until="domcontentloaded")
            await asyncio.sleep(2)
        else:
            console.print(f"[dim]Browser already on page: {current_url}[/dim]")

    async def get_screenshot_base64(self, save_path: Optional[Path] = None) -> str:
        """Captures a screenshot of the viewport and returns base64 string."""
        if save_path:
            save_path.parent.mkdir(parents=True, exist_ok=True)
        img_bytes = await self.page.screenshot(path=str(save_path) if save_path else None, full_page=False)
        return base64.b64encode(img_bytes).decode("utf-8")

    async def extract_interactive_dom(self) -> Dict[str, Any]:
        """Extracts text, headings, buttons, and form/quiz elements. The sidebar is excluded from everything."""
        try:
            return await self.page.evaluate(EXTRACT_JS)
        except Exception as e:
            try:
                title = await self.page.title()
            except Exception:
                title = ""
            return {"error": str(e), "url": self.page.url, "title": title}

    # ------------------------------------------------------------------ clicking

    async def _click_xy(self, x: float, y: float) -> bool:
        """THE single choke point for every mouse click. Refuses to click anything inside the sidebar."""
        try:
            if await self._in_sidebar(x, y):
                console.print(f"[bold red]⛔ Refused click at ({int(x)}, {int(y)}): target is inside the sidebar.[/bold red]")
                return False
            await self.page.mouse.move(x, y)
            await self.page.mouse.down()
            await asyncio.sleep(0.05)
            await self.page.mouse.up()
            return True
        except Exception as e:
            console.print(f"[yellow]Click at ({x}, {y}) failed: {e}[/yellow]")
            return False

    async def _in_sidebar(self, x: float, y: float) -> bool:
        return bool(await self.page.evaluate(_js("""
            ([x, y]) => {
                /*H*/
                markSidebar();
                return inSidebar(document.elementFromPoint(x, y));
            }
        """), [x, y]))

    async def mark_sidebar(self) -> None:
        await self.page.evaluate(_js("() => { /*H*/ markSidebar(); }"))

    async def dismiss_popups(self) -> bool:
        """Dismisses overlays ('Maybe later', modal close buttons) in a single scan per attempt."""
        dismissed = False
        for _ in range(3):
            try:
                hit = await self.page.evaluate(POPUP_JS)
                if not hit or not await self._click_xy(hit["x"], hit["y"]):
                    break
                console.print(f"[dim]Auto-dismissed popup ('{hit['text']}')[/dim]")
                dismissed = True
                await asyncio.sleep(0.4)
            except Exception:
                break
        return dismissed

    async def click_by_text(self, text: str, exact: bool = False, require_enabled: bool = False) -> bool:
        """Clicks the best element matching text. Exact matches win over substring ones; sidebar is never considered."""
        clean_text = text.strip()
        if not clean_text:
            return False
        try:
            await self.mark_sidebar()

            # Strategy 1: Playwright locators, strictest match first so 'Next' can't hit 'Best next step'.
            attempts = [("button", True), ("link", True), ("text", True), ("button", False), ("link", False)]
            if len(clean_text) >= 12:
                attempts.append(("text", False))
            for ctx in self.page.frames:
                for kind, is_exact in attempts:
                    try:
                        if kind == "text":
                            loc = ctx.get_by_text(clean_text, exact=is_exact)
                        else:
                            loc = ctx.get_by_role(kind, name=clean_text, exact=is_exact)
                        for i in range(min(await loc.count(), 8)):
                            el = loc.nth(i)
                            try:
                                if not await el.evaluate(CHECK_EL_JS, require_enabled):
                                    continue
                                await el.scroll_into_view_if_needed(timeout=2000)
                                box = await el.bounding_box()
                                if box and box["width"] > 0 and box["height"] > 0:
                                    if await self._click_xy(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2):
                                        return True
                            except Exception:
                                continue
                    except Exception:
                        continue

            # Strategy 2: fuzzy scan over all rendered elements (pills/divs that aren't buttons).
            res = await self.page.evaluate(FIND_TEXT_JS, {"target": clean_text, "requireEnabled": require_enabled})
            if res and res.get("success"):
                return await self._click_xy(res["x"], res["y"])
        except Exception as e:
            console.print(f"[yellow]Click by text '{text}' failed: {e}[/yellow]")
        return False

    async def click_by_selector(self, selector: str) -> bool:
        """Clicks the first visible, non-sidebar element matching a CSS selector."""
        try:
            await self.mark_sidebar()
            loc = self.page.locator(selector)
            for i in range(min(await loc.count(), 8)):
                el = loc.nth(i)
                try:
                    if not await el.is_visible() or not await el.evaluate(CHECK_EL_JS, False):
                        continue
                    await el.scroll_into_view_if_needed(timeout=1500)
                    box = await el.bounding_box()
                    if box and box["width"] > 0 and box["height"] > 0:
                        if await self._click_xy(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2):
                            return True
                except Exception:
                    continue
        except Exception as e:
            console.print(f"[yellow]Click selector '{selector}' notice: {e}[/yellow]")
        return False

    # ------------------------------------------------------------------ matching exercise

    async def click_matching_card(self, target_text: str) -> bool:
        """Scrolls to and clicks an exercise card (skips it if it already looks selected)."""
        try:
            info = await self.page.evaluate(FIND_CARD_JS, {"target": target_text.strip(), "scroll": True})
            if info:
                if info.get("alreadyActive"):
                    console.print(f"[dim]Card already active/selected: '{target_text[:35]}...'[/dim]")
                    return True
                if await self._click_xy(info["x"], info["y"]):
                    await asyncio.sleep(0.3)
                    return True
                return False
            return await self.click_by_text(target_text, exact=False, require_enabled=False)
        except Exception as e:
            console.print(f"[dim]Card click notice for '{target_text[:30]}': {e}[/dim]")
            return False

    async def click_matching_pair(self, scenario_text: str, match_text: str) -> bool:
        """Click-style matching ('N of M matched'): click the left card, then its partner on the right."""
        console.print(f"[bold cyan]Connecting pair:[/bold cyan]\n  Left:  '{scenario_text[:45]}...'\n  Right: '{match_text[:45]}...'")
        left_ok = await self.click_matching_card(scenario_text)
        await asyncio.sleep(0.3)
        right_ok = await self.click_matching_card(match_text)
        await asyncio.sleep(0.3)
        return left_ok or right_ok

    # --- drag-to-reorder matching: rows are fixed on one side, cards on the other side are dragged into the right row ---

    async def read_matching_cards(self) -> Optional[Tuple[List[str], List[str]]]:
        """(left card texts, right card texts) in on-screen order, or None when the columns don't pair up."""
        try:
            cards = (await self.page.evaluate(GET_CARDS_JS))["cards"]
        except Exception:
            return None
        col = lambda c: [x["text"] for x in sorted((x for x in cards if x["col"] == c), key=lambda x: x["cy"])]
        left, right = col("L"), col("R")
        return (left, right) if len(left) == len(right) >= 2 else None

    @staticmethod
    def _best_card(cards: List[Dict[str, Any]], text: str) -> Optional[Dict[str, Any]]:
        words = set(re.findall(r"\w{2,}", text.lower()))
        best, best_j = None, 0.0
        for c in cards:
            cw = set(re.findall(r"\w{2,}", c["text"].lower()))
            if words and cw:
                j = len(words & cw) / len(words | cw)
                if j > best_j:
                    best, best_j = c, j
        return best if best_j >= 0.3 else None

    async def _drag(self, x1: float, y1: float, x2: float, y2: float) -> None:
        m = self.page.mouse
        await m.move(x1, y1)
        await m.down()
        await asyncio.sleep(0.08)
        await m.move(x1, y1 + (6 if y2 > y1 else -6), steps=2)   # get past drag-activation distance
        for s in range(1, 21):
            await m.move(x1 + (x2 - x1) * s / 20, y1 + (y2 - y1) * s / 20)
            await asyncio.sleep(0.015)
        await asyncio.sleep(0.2)
        await m.up()
        await asyncio.sleep(0.5)

    async def solve_by_reorder(self, pairs: List[Tuple[str, str]]) -> bool:
        """pairs = (left card text, right card text). Arranges the page so every pair shares a row. True when arranged."""
        order = [self._drag_side] if self._drag_side else []
        order += [s for s in ("R", "L") if s not in order]
        for move in order:
            res = await self._reorder(pairs, move)
            if res is None:          # this column does not respond to dragging: try the other one
                continue
            if res:
                self._drag_side = move
            return res
        return False

    @staticmethod
    def _dump_cards(cards: List[Dict[str, Any]]) -> None:
        for c in sorted(cards, key=lambda c: (c["col"], c["cy"])):
            console.print(f"[dim]  {c['col']} y={c['cy']:<5} '{c['text'][:60]}'[/dim]")

    async def _reorder(self, pairs: List[Tuple[str, str]], move: str) -> Optional[bool]:
        """Drags cards of column `move` so each lands on the row of its partner in the other column.
        Only the cards named by `pairs` are used; any other text on the page is ignored. None = column not draggable."""
        anchor = "L" if move == "R" else "R"
        data = await self.page.evaluate(GET_CARDS_JS)
        cards = data["cards"]

        picks = []   # (left card, right card) per pair, chosen by text similarity within each column
        for lt, rt in pairs:
            lc = self._best_card([c for c in cards if c["col"] == "L"], lt)
            rc = self._best_card([c for c in cards if c["col"] == "R"], rt)
            if not lc or not rc:
                console.print(f"[yellow]Reorder: no on-screen card matches pair '{lt[:35]}...' -> '{rt[:35]}...'. Cards seen:[/yellow]")
                self._dump_cards(cards)
                return False
            picks.append((lc, rc))
        if len({l["text"] for l, _ in picks}) != len(picks) or len({r["text"] for _, r in picks}) != len(picks):
            console.print("[yellow]Reorder: two pairs resolve to the same card (model text too vague). Cards seen:[/yellow]")
            self._dump_cards(cards)
            return False

        a_texts = {(l if move == "R" else r)["text"] for l, r in picks}
        m_texts = {(r if move == "R" else l)["text"] for l, r in picks}

        def view(d):
            def col(name, texts):
                uniq = {}
                for c in d["cards"]:
                    if c["col"] == name and c["text"] in texts:
                        uniq.setdefault(c["text"], c)
                return sorted(uniq.values(), key=lambda c: c["cy"])
            return col(anchor, a_texts), col(move, m_texts)

        anchors, movers = view(data)
        if len(anchors) != len(picks) or len(movers) != len(picks):
            console.print(f"[yellow]Reorder: expected {len(picks)} cards per column, found {len(anchors)} and {len(movers)}. Cards seen:[/yellow]")
            self._dump_cards(cards)
            return False

        row_of = {c["text"]: i for i, c in enumerate(anchors)}
        target = {(r if move == "R" else l)["text"]: row_of[(l if move == "R" else r)["text"]] for l, r in picks}

        for attempt in range(2 * len(picks) + 2):
            data = await self.page.evaluate(GET_CARDS_JS)
            anchors, movers = view(data)
            if len(anchors) != len(picks) or len(movers) != len(picks):
                return False
            rows = [target[c["text"]] for c in movers]
            if rows == list(range(len(rows))):
                console.print("[bold green]✓ All cards are on their matching rows.[/bold green]")
                return True
            i = next(k for k, v in enumerate(rows) if v != k)        # first wrong row (top-down keeps finished rows stable)
            src = movers[rows.index(i)]
            dst_y = anchors[i]["cy"] - 12   # always an upward move: land just above the row centre so the slot is unambiguous
            console.print(f"[yellow]Row {i + 1}: dragging '{src['text'][:40]}...' to y={dst_y} ({'right' if move == 'R' else 'left'} column)[/yellow]")

            grabs = [(src["cx"], src["cy"])]
            handles = [e for e in data["eqs"] if abs(e["cy"] - src["cy"]) < 40]
            if handles:
                grabs.append((handles[0]["cx"], handles[0]["cy"]))     # the '=' knob may be the drag handle
            before = [c["text"] for c in movers]
            moved = False
            for gx, gy in grabs:
                if await self._in_sidebar(gx, gy):
                    continue
                await self._drag(gx, gy, gx, dst_y)
                _, after = view(await self.page.evaluate(GET_CARDS_JS))
                if [c["text"] for c in after] != before:
                    moved = True
                    break
            if not moved:
                return None if attempt == 0 else False
        return False

    # ------------------------------------------------------------------ quiz

    async def select_quiz_option(self, option_text: str, option_index: Optional[int] = None) -> bool:
        """Selects an MCQ option with ONE native click, then verifies the state (never double-toggles checkboxes)."""
        clean_text = option_text.strip()
        try:
            res = await self.page.evaluate(FIND_OPTION_JS, {"target": clean_text, "targetIndex": option_index})
            if res and res.get("success"):
                if res.get("alreadySelected"):
                    console.print(f"[dim]Option already selected: '{clean_text[:40]}...'[/dim]")
                    return True
                if not await self._click_xy(res["x"], res["y"]):
                    await self.page.evaluate("() => document.querySelectorAll('[data-agent-target]').forEach(e => e.removeAttribute('data-agent-target'))")
                    return False
                await asyncio.sleep(0.15)
                v = await self.page.evaluate(VERIFY_OPTION_JS)
                if v.get("retried"):
                    console.print("[dim]Native click did not register; re-clicked option via DOM.[/dim]")
                return True
            return await self.click_by_text(clean_text, exact=False, require_enabled=False)
        except Exception as e:
            console.print(f"[yellow]Select quiz option failed for '{option_text}': {e}[/yellow]")
            return False

    # ------------------------------------------------------------------ viewport / scrolling

    async def get_viewport_metrics(self) -> Dict[str, Any]:
        """Current viewport dimensions, scroll progress, and DPI scaling."""
        try:
            return await self.page.evaluate("""
                () => ({
                    width: window.innerWidth,
                    height: window.innerHeight,
                    devicePixelRatio: window.devicePixelRatio || 1,
                    scrollX: window.scrollX,
                    scrollY: window.scrollY,
                    scrollHeight: Math.max(document.body.scrollHeight, document.documentElement.scrollHeight),
                    centerX: Math.round(window.innerWidth / 2),
                    centerY: Math.round(window.innerHeight / 2)
                })
            """)
        except Exception:
            return {"width": 1440, "height": 900, "devicePixelRatio": 1, "centerX": 720, "centerY": 450}

    async def click_at_point(self, x: int, y: int) -> bool:
        """Clicks viewport coordinates (clamped on-screen). Refused if the point lies in the sidebar."""
        m = await self.get_viewport_metrics()
        return await self._click_xy(max(5, min(x, m.get("width", 1440) - 5)), max(5, min(y, m.get("height", 900) - 5)))

    async def _scroll(self, delta: int) -> None:
        """Trusted wheel scroll over the main content; falls back to scrolling the main scroller via JS if nothing moved."""
        try:
            m = await self.get_viewport_metrics()
            pos_js = _js("() => { /*H*/ return scroller().scrollTop; }")
            before = await self.page.evaluate(pos_js)
            await self.page.mouse.move(m["centerX"], m["centerY"])
            await self.page.mouse.wheel(0, delta)
            await asyncio.sleep(0.1)
            if await self.page.evaluate(pos_js) == before:
                await self.page.evaluate(_js("(d) => { /*H*/ scroller().scrollBy(0, d); }"), delta)
        except Exception as e:
            console.print(f"[dim]Scroll notice: {e}[/dim]")
        await asyncio.sleep(0.2)

    async def scroll_down(self, delta: Optional[int] = None) -> None:
        m = await self.get_viewport_metrics()
        await self._scroll(delta or int(m.get("height", 900) * 0.75))

    async def scroll_up(self, delta: Optional[int] = None) -> None:
        m = await self.get_viewport_metrics()
        await self._scroll(-(delta or int(m.get("height", 900) * 0.75)))

    async def scroll_to_bottom(self) -> None:
        """Scrolls the main content (not the sidebar) to the bottom and centres the progression button."""
        try:
            m = await self.get_viewport_metrics()
            await self.page.mouse.move(m["centerX"], m["centerY"])
            for _ in range(3):
                await self.page.mouse.wheel(0, 1500)
                await asyncio.sleep(0.04)
            await self.page.evaluate(_js(r"""
                () => {
                    /*H*/
                    markSidebar();
                    window.scrollTo(0, 999999);
                    scroller().scrollTop = 999999;
                    document.querySelectorAll('*').forEach(el => {
                        if (el.scrollHeight > el.clientHeight + 10 && !inSidebar(el) && /(auto|scroll)/.test(getComputedStyle(el).overflowY)) {
                            el.scrollTop = el.scrollHeight;
                        }
                    });
                    const btn = Array.from(document.querySelectorAll('button, a, [role="button"]')).reverse()
                        .find(b => !inSidebar(b) && isRendered(b) && /^(lesson\s+\d+|next|continue|complete|submit)/i.test(clean(b)));
                    if (btn) btn.scrollIntoView({ behavior: 'instant', block: 'center' });
                }
            """))
        except Exception as e:
            console.print(f"[dim]Scroll to bottom notice: {e}[/dim]")
        await asyncio.sleep(0.3)

    async def ensure_interactive_content_in_view(self) -> bool:
        """Aligns an active quiz/matching exercise so the whole prompt and its options are visible. True if it scrolled."""
        try:
            return await self.page.evaluate(_js(r"""
                () => {
                    /*H*/
                    markSidebar();
                    const align = (el) => {
                        const top = el.getBoundingClientRect().top;
                        if (top < 70 || top > 250) {
                            el.scrollIntoView({ behavior: 'instant', block: 'start' });
                            window.scrollBy(0, -90);   // sticky navbar
                            return true;
                        }
                        return false;
                    };
                    const q = findText(/^(question\s*\d+|\d+\/\d+)$/i) || findText(/^question\s*\d+/i);
                    if (q) return align(q);

                    const opt = Array.from(document.querySelectorAll('input[type="radio"], input[type="checkbox"], [role="radio"], [role="checkbox"]'))
                        .find(e => !inSidebar(e));
                    if (opt) {
                        const top = opt.getBoundingClientRect().top;
                        if (top < 150 || top > window.innerHeight - 100) {
                            opt.scrollIntoView({ behavior: 'instant', block: 'center' });
                            return true;
                        }
                    }
                    const m = findText(/^\d+\s+of\s+\d+\s+matched/i) || findText(/select either side to start/i) || findText(/select the most appropriate way/i) || findText(/^match the pairs$/i);
                    return m ? align(m) : false;
                }
            """))
        except Exception:
            return False

    async def wait_for_idle(self, timeout_sec: float = 0.5) -> None:
        """Waits briefly for network and DOM animations to settle."""
        try:
            await self.page.wait_for_load_state("networkidle", timeout=timeout_sec * 1000)
        except Exception:
            await asyncio.sleep(timeout_sec)

    async def close(self) -> None:
        """Closes our own browser; when attached over CDP we only disconnect and leave the user's Chrome alone."""
        if self.context and not settings.cdp_url:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()
