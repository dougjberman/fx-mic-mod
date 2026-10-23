/* Shared motion for the fx-mic-mod site: the sliding nav pill and scroll reveals.
   Loaded at the end of <body> so the pill is placed before the first paint. */
(() => {
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Nav pill: one element that sits behind the current tab, so a view transition can slide it.
  const nav = document.querySelector(".site-nav"), ul = nav && nav.querySelector("ul");
  const cur = nav && nav.querySelector('a.tab[aria-current="page"]');
  if (ul && cur) {
    const pill = document.createElement("li");
    pill.className = "pill"; pill.setAttribute("aria-hidden", "true");
    ul.prepend(pill);
    const place = () => {
      pill.style.transform = `translate(${cur.offsetLeft}px, ${cur.offsetTop}px)`;
      pill.style.width = cur.offsetWidth + "px"; pill.style.height = cur.offsetHeight + "px";
    };
    place(); nav.classList.add("has-pill");
    addEventListener("resize", place);
    if (document.fonts) document.fonts.ready.then(place);
  }

  if (reduce) return;
  document.documentElement.classList.add("motion");

  // Scroll reveals. Children of a [data-reveal-list] stagger; the delay is capped at six steps.
  if (!("IntersectionObserver" in window)) { document.querySelectorAll("[data-reveal],[data-reveal-list] > *").forEach(el => el.classList.add("in")); return; }
  document.querySelectorAll("[data-reveal-list]").forEach(list =>
    [...list.children].forEach((el, i) => { el.setAttribute("data-reveal", ""); el.style.setProperty("--i", Math.min(i, 6)); }));
  const els = document.querySelectorAll("[data-reveal]");
  const io = new IntersectionObserver(entries => {
    for (const e of entries) if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
  }, { rootMargin: "0px 0px -8% 0px", threshold: 0.12 });
  els.forEach(el => io.observe(el));
})();
