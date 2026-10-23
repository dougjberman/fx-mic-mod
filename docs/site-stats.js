/* Visit and button counts, through GoatCounter (goatcounter.com): no cookies,
 * no personal data, and the part of a URL after # is never sent, so shared
 * presets stay out of the stats. Nothing loads until CODE is set.
 * Pages call track("name") for button presses; it does nothing without stats. */
(function(){
  var CODE = "fx-mic-mod";   // the GoatCounter site code, as in https://CODE.goatcounter.com
  window.track = function(name){
    try { if (window.goatcounter && window.goatcounter.count) window.goatcounter.count({path:name, title:name, event:true}); } catch(e){}
  };
  if (!CODE) return;
  var s = document.createElement("script");
  s.async = true;
  s.src = "https://gc.zgo.at/count.js";
  s.setAttribute("data-goatcounter", "https://" + CODE + ".goatcounter.com/count");
  document.head.appendChild(s);
})();
