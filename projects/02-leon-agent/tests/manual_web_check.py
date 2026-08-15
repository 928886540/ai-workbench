"""End-to-end verification of the four web-client fixes against the live gateway."""

import os
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8233"
TOKEN = os.environ["LEON_TOKEN"]
IMGS = [f"{BASE}/icon.svg?album={i}" for i in (1, 2, 3)]

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=os.environ.get("CHROME_PATH") or None)
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844},
        device_scale_factor=3,
        has_touch=True,
        is_mobile=True,
    )
    ctx.add_init_script(f"localStorage.setItem('leon_token', {TOKEN!r})")
    page = ctx.new_page()
    errors = []
    bad_responses = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on(
        "response",
        lambda r: bad_responses.append(f"{r.status} {r.url}") if r.status >= 400 else None,
    )

    page.goto(BASE, wait_until="networkidle")
    page.wait_for_selector("#login-screen.hidden", state="attached", timeout=15000)
    check("app 初始化完成（登录页隐藏）", True)

    # ---------------- Bug 3: model picker ----------------
    page.click('.nav-item[data-page="settings"]')
    page.wait_for_selector("#model-list .model-option", timeout=30000)
    count = page.locator("#model-list .model-option").count()
    check("设置页模型列表渲染出可点条目", count > 0, f"{count} 个模型")

    status = page.inner_text("#model-status")
    check("模型状态栏显示数量而非空白", "可用模型" in status, repr(status))

    target = page.locator("#model-list .model-option").nth(1)
    picked = target.inner_text().split("\n")[0].strip()
    target.click()
    val = page.input_value("#model-input")
    check("点击模型条目写入输入框", val == picked, f"输入框={val!r}")
    check(
        "被点条目高亮为 selected",
        page.locator("#model-list .model-option.selected").count() == 1,
    )

    # Derive the filter keyword from the live catalogue — the active provider
    # (and therefore the model names) can change between runs.
    keyword = page.evaluate("() => modelCatalog[0].replace(/[^A-Za-z]/g,'').slice(0,5)")
    page.fill("#model-input", keyword)
    filtered = page.locator("#model-list .model-option").count()
    check(
        "输入关键字可过滤列表",
        0 < filtered <= count,
        f"{filtered}/{count} 条匹配 {keyword!r}",
    )

    # ---------------- Bug 2: finished image becomes a new bottom bubble ----------------
    page.click('.nav-item[data-page="chat"]')
    layout = page.evaluate(
        """([urls]) => {
        addImageSkeleton('job-x');
        addBubble('agent','LLM 在骨架屏之后回复的一句话',false);
        return new Promise(resolve => {
          replaceSkeletonWithImage('job-x', urls[0]);
          setTimeout(() => {
            const kids=[...$msgs.children];
            resolve({
              last: kids[kids.length-1].className,
              skeletonLeft: document.querySelectorAll('.image-placeholder').length,
              imageIsLast: kids[kids.length-1].querySelector('img') !== null,
            });
          }, 1200);
        });
      }""",
        [IMGS],
    )
    check("骨架屏已移除", layout["skeletonLeft"] == 0)
    check(
        "图片作为最后一个气泡出现在底部",
        layout["imageIsLast"] and "bubble-wrap" in layout["last"],
        f"末元素={layout['last']!r}",
    )

    # The user scrolls away during a long generation; the finished image must still
    # pull the view down to itself instead of landing silently off-screen.
    scrolled = page.evaluate(
        """([urls]) => {
        $msgs.innerHTML='';
        for(let i=0;i<40;i++) addBubble('agent','填充消息 '+i,false);
        addImageSkeleton('job-scroll');
        for(let i=0;i<10;i++) addBubble('agent','骨架屏之后的回复 '+i,false);
        $msgs.scrollTop=0;
        $msgs.dispatchEvent(new Event('scroll'));
        const away=autoFollowMessages;
        return new Promise(resolve => {
          replaceSkeletonWithImage('job-scroll', urls[0]);
          setTimeout(() => resolve({
            away,
            gap: $msgs.scrollHeight-$msgs.scrollTop-$msgs.clientHeight,
            imageVisible: (() => {
              const img=[...$msgs.querySelectorAll('img')].pop();
              if(!img) return false;
              const r=img.getBoundingClientRect(), m=$msgs.getBoundingClientRect();
              return r.bottom<=m.bottom+2 && r.top<m.bottom;
            })(),
          }), 1500);
        });
      }""",
        [IMGS],
    )
    check("用户滚离底部时 autoFollow 确实关闭", scrolled["away"] is False)
    check("图片到达后自动滚到底部", scrolled["gap"] < 4, f"距底部 {scrolled['gap']:.1f}px")
    check("新图片完整落在可视区内", scrolled["imageVisible"])

    # ---------------- Bug 1 + 4: album viewer ----------------
    page.evaluate(
        """([urls]) => {
        $msgs.innerHTML='';
        urls.forEach(u => {
          const w=document.createElement('div');w.className='bubble-wrap agent';
          w.innerHTML=`<div class="bubble agent"><a class="markdown-image-link" href="${u}">`+
                      `<img class="markdown-image" src="${u}" alt="生成图片"></a></div>`;
          $msgs.appendChild(w);
        });
      }""",
        [IMGS],
    )
    page.wait_for_function(
        "() => [...document.querySelectorAll('#chat-messages img')]"
        ".every(i => i.complete && i.naturalWidth > 0)",
        timeout=15000,
    )
    page.locator("#chat-messages img").first.click()
    page.wait_for_selector("#image-viewer:not([hidden])", timeout=5000)

    album = page.evaluate("() => ({n: ivAlbum.length, i: ivIndex})")
    check("查看器把会话全部图片收成相册", album["n"] == 3, f"相册张数={album['n']}")
    check("计数器显示 1 / 3", page.inner_text("#image-viewer-counter") == "1 / 3")

    page.click("#image-viewer-next")
    page.click("#image-viewer-next")
    at3 = page.inner_text("#image-viewer-counter")
    page.click("#image-viewer-next")
    looped = page.inner_text("#image-viewer-counter")
    check("下一张走到 3 / 3", at3 == "3 / 3", at3)
    check("末张再下一张循环回 1 / 3", looped == "1 / 3", looped)
    page.click("#image-viewer-prev")
    back = page.inner_text("#image-viewer-counter")
    check("首张上一张反向循环到 3 / 3", back == "3 / 3", back)

    # Bug 1a: the close button must stay hit-testable when the image is zoomed in.
    page.evaluate("() => zoomFromCenter(4)")
    hit = page.evaluate(
        """() => {
        const r=document.getElementById('image-viewer-close').getBoundingClientRect();
        const el=document.elementFromPoint(r.left+r.width/2, r.top+r.height/2);
        return {id: el && el.id, tag: el && el.tagName, scale: viewerScale};
      }"""
    )
    check(
        "放大 4 倍后关闭按钮未被图片遮挡",
        hit["id"] == "image-viewer-close",
        f"该点命中 <{hit['tag']} id={hit['id']}> scale={hit['scale']}",
    )

    closed = page.evaluate(
        """() => {
        document.getElementById('image-viewer-close').click();
        return document.getElementById('image-viewer').hidden;
      }"""
    )
    check("放大状态下点击关闭按钮真的能关闭", closed)

    # Bug 1b: pinch/wheel zoom must pin the point under the cursor, not the centre.
    # Pick a focal point whose required offset stays inside the pan clamp, so this
    # measures the focal maths rather than the (separately tested) bounds clamp.
    page.locator("#chat-messages img").first.click()
    page.wait_for_selector("#image-viewer:not([hidden])", timeout=5000)
    focal = page.evaluate(
        """() => {
        resetViewerZoom();
        const scale=4, r0=$imageViewerImage.getBoundingClientRect();
        const maxX=Math.max(0,($imageViewerImage.clientWidth*scale-$imageViewer.clientWidth)/2);
        const maxY=Math.max(0,($imageViewerImage.clientHeight*scale-$imageViewer.clientHeight)/2);
        // starting from offset 0, focal zoom needs offset = -(scale-1)*d
        const dx=0.8*maxX/(scale-1), dy=0.8*maxY/(scale-1);
        const cx=r0.left+r0.width/2, cy=r0.top+r0.height/2;
        const fx=cx+dx, fy=cy+dy;
        const u=(fx-r0.left)/r0.width, v=(fy-r0.top)/r0.height;
        zoomAt(scale, fx, fy);
        const r1=$imageViewerImage.getBoundingClientRect();
        return {
          driftX: Math.abs(r1.left+u*r1.width-fx),
          driftY: Math.abs(r1.top+v*r1.height-fy),
          scale: viewerScale, maxX, maxY,
          clamped: Math.abs(viewerX)>=maxX-0.5||Math.abs(viewerY)>=maxY-0.5,
        };
      }"""
    )
    check(
        "定点缩放：光标下的像素保持不动",
        focal["driftX"] < 1.0 and focal["driftY"] < 1.0 and not focal["clamped"],
        f"漂移 dx={focal['driftX']:.3f}px dy={focal['driftY']:.3f}px "
        f"@scale={focal['scale']} 未触边界={not focal['clamped']}",
    )

    # A focal point that would push past the edge must be clamped, not honoured.
    edge = page.evaluate(
        """() => {
        resetViewerZoom();
        const r=$imageViewerImage.getBoundingClientRect();
        zoomAt(4, r.left+r.width*0.02, r.top+r.height*0.02);
        const maxX=Math.max(
          0,($imageViewerImage.clientWidth*viewerScale-$imageViewer.clientWidth)/2);
        const maxY=Math.max(
          0,($imageViewerImage.clientHeight*viewerScale-$imageViewer.clientHeight)/2);
        return {x:viewerX,y:viewerY,maxX,maxY};
      }"""
    )
    check(
        "极端定点缩放被夹在边界内，不会露出空白",
        abs(edge["x"]) <= edge["maxX"] + 0.51 and abs(edge["y"]) <= edge["maxY"] + 0.51,
        f"offset=({edge['x']:.0f},{edge['y']:.0f}) 上限=({edge['maxX']:.0f},{edge['maxY']:.0f})",
    )

    # Bug 1c: vertical panning must actually move the image once it overflows.
    pan = page.evaluate(
        """() => {
        resetViewerZoom(); zoomFromCenter(4);
        const before=viewerY;
        viewerY -= 400; applyViewerTransform();
        const after=viewerY;
        const h=$imageViewerImage.clientHeight*viewerScale;
        const maxY=Math.max(0,(h-$imageViewer.clientHeight)/2);
        return {before, after, maxY, moved: Math.abs(after-before)};
      }"""
    )
    check(
        "放大后可上下平移",
        pan["moved"] > 0,
        f"纵向位移 {pan['moved']:.0f}px（上限 {pan['maxY']:.0f}px）",
    )
    check(
        "平移被夹在图片边界内，不会拖飞",
        abs(pan["after"]) <= pan["maxY"] + 0.51,
        f"y={pan['after']:.1f} 上限={pan['maxY']:.1f}",
    )

    real_errors = [
        e for e in errors if "favicon" not in e.lower() and "404" not in e
    ]
    check("运行期无 JS 报错", not real_errors, "; ".join(real_errors[:3]))
    check(
        "无 4xx/5xx 资源请求",
        not bad_responses,
        "; ".join(sorted(set(bad_responses))[:5]),
    )

    browser.close()

failed = [r for r in results if not r[1]]
print(f"\n{'=' * 60}\n{len(results) - len(failed)}/{len(results)} 通过")
if failed:
    print("失败项：")
    for name, _, detail in failed:
        print(f"  - {name}  {detail}")
sys.exit(1 if failed else 0)
