/* Which mic the visitor has: the EP-2350 is sold as the FX Mic and as the
 * Ting, with the same insides and different colors:
 *
 *              FX Mic    Ting
 *   top        orange    orange
 *   middle     white     green
 *   bottom     gray      white
 *   handle     orange    green
 *
 * Loaded in <head> so <html data-device> is set before the page paints. The
 * choice is remembered, and ?device=ting or ?device=fxmic in a link sets it.
 * Words that differ sit in <span data-dw="mid"> and the like; site-device.css
 * does the colors and swaps the drawings. */
(function(){
  var KEY = "fx-mic-mod-device";
  var WORDS = {
    mid:    {fxmic: "white",  ting: "green"},
    Mid:    {fxmic: "White",  ting: "Green"},
    bot:    {fxmic: "gray",   ting: "white"},
    Bot:    {fxmic: "Gray",   ting: "White"},
    handle: {fxmic: "orange", ting: "green"},
    name:   {fxmic: "FX Mic", ting: "Ting"},
    NAME:   {fxmic: "FX MIC", ting: "TING"}
  };
  var root = document.documentElement;

  function read(){
    var m = /[?&]device=(ting|fxmic)\b/.exec(location.search);
    if (m) return m[1];
    try { var v = localStorage.getItem(KEY); if (v === "ting" || v === "fxmic") return v; } catch(e){}
    return "fxmic";
  }
  function word(key){ var w = WORDS[key]; return w ? w[root.dataset.device] : key; }
  function apply(){
    var els = document.querySelectorAll("[data-dw]");
    for (var i = 0; i < els.length; i++) els[i].textContent = word(els[i].getAttribute("data-dw"));
    els = document.querySelectorAll(".device-switch button");
    for (i = 0; i < els.length; i++) els[i].setAttribute("aria-pressed", String(els[i].value === root.dataset.device));
  }
  function set(device){
    if (device === root.dataset.device) return;
    root.dataset.device = device;
    try { localStorage.setItem(KEY, device); } catch(e){}
    apply();
    if (window.track) window.track("device-" + device);
    document.dispatchEvent(new CustomEvent("devicechange", {detail: device}));
  }

  root.dataset.device = read();
  window.siteDevice = {get: function(){ return root.dataset.device; }, set: set, word: word};

  // The switcher sits in the site nav on every page.
  function build(){
    var nav = document.querySelector(".site-nav");
    if (nav && !nav.querySelector(".device-switch")) {
      var box = document.createElement("div");
      box.className = "device-switch";
      box.setAttribute("role", "group");
      box.setAttribute("aria-label", "Select your device");
      box.innerHTML = '<span class="ds-label"><span class="ds-long">Select your device</span><span class="ds-short">Device</span></span>' +
        '<button type="button" value="fxmic"><i class="sw sw-fxmic" aria-hidden="true"></i>FX Mic</button>' +
        '<button type="button" value="ting"><i class="sw sw-ting" aria-hidden="true"></i>Ting</button>';
      box.addEventListener("click", function(e){
        var b = e.target.closest("button");
        if (b) set(b.value);
      });
      var brand = nav.querySelector(".brand");
      brand.parentNode.insertBefore(box, brand.nextSibling);
    }
    apply();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", build);
  else build();
})();
