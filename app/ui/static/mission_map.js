/*
 * Shared mission-map drawing (Google Maps JavaScript API), used by BOTH
 *   - app/ui/static/mission_map.html     (CLI map: scripts/plan_mission.py)
 *   - app/ui/static/mission_control.html (web UI: app/api/main.py)
 * It draws ONLY what the backend's mission view says (app/ui/mission_view.py):
 *   red    = real building outline (or a "T" pin when only a point is known)
 *   red    = inspection region (the same ring the planner was given; follows the outline)
 *   orange = neighbouring buildings the route avoids (white = height not mapped)
 *   blue   = planned route + waypoints        green  = completed / flown
 *   yellow = RAMR replanning / corrective     dark dot = drone (telemetry)
 * Markers use AdvancedMarkerElement (google.maps.Marker is deprecated).
 */
"use strict";
window.MissionMap = (() => {
  let map = null, lib = {}, overlays = [], ready = false, current = null;
  let lastFitKey = null, lastPickFit = null, clickListenerSet = false, pickMarker = null, pickHandler = null;

  const ll = p => ({ lat: p[0], lng: p[1] });

  async function init(cfg, element) {
    // Google's documented dynamic library import bootstrap loader.
    (g=>{var h,a,k,p="The Google Maps JavaScript API",c="google",l="importLibrary",q="__ib__",m=document,b=window;b=b[c]||(b[c]={});var d=b.maps||(b.maps={}),r=new Set,e=new URLSearchParams,u=()=>h||(h=new Promise(async(f,n)=>{await (a=m.createElement("script"));e.set("libraries",[...r]+"");for(k in g)e.set(k.replace(/[A-Z]/g,t=>"_"+t[0].toLowerCase()),g[k]);e.set("callback",c+".maps."+q);a.src=`https://maps.${c}apis.com/maps/api/js?`+e;d[q]=f;a.onerror=()=>h=n(Error(p+" could not load."));a.nonce=m.querySelector("script[nonce]")?.nonce||"";m.head.append(a)}));d[l]?console.warn(p+" only loads once. Ignoring:",g):d[l]=(f,...n)=>r.add(f)&&u().then(()=>d[l](f,...n))})({ key: cfg.browser_api_key, v: "weekly" });

    const { Map, Polygon, Polyline, Circle } = await google.maps.importLibrary("maps");
    const { AdvancedMarkerElement, PinElement } = await google.maps.importLibrary("marker");
    const { LatLngBounds } = await google.maps.importLibrary("core");
    lib = { Polygon, Polyline, Circle, AdvancedMarkerElement, PinElement, LatLngBounds };
    // No hard-coded starting location: world view until a mission arrives.
    map = new Map(element, { center: { lat: 0, lng: 0 }, zoom: 2, mapId: cfg.map_id, mapTypeId: "hybrid", tilt: 0, streetViewControl: false });
    ready = true;
    if (current) { lastFitKey = null; lastPickFit = null; render(current); }
  }

  function clearOverlays() { for (const o of overlays) { if (o.setMap) o.setMap(null); else o.map = null; } overlays = []; }

  function pin(color, glyph, title, pos, z) {
    const p = new lib.PinElement({ background: color, borderColor: "#ffffff", glyphColor: "#ffffff", glyph: String(glyph) });
    const m = new lib.AdvancedMarkerElement({ map, position: pos, content: p.element, title, zIndex: z || 1 });
    overlays.push(m); return m;
  }

  function draw(v) {
    clearOverlays();
    const C = v.colors, regionColor = v.replanning_active ? C.replanning : C.target;
    const bounds = new lib.LatLngBounds(); let hasBounds = false;
    const extend = p => { bounds.extend(p); hasBounds = true; };

    // RED target: real footprint polygon(s) only. A point stays a point -- no invented outline.
    if (v.target) {
      if (v.target.footprint && v.target.footprint.length) {
        for (const ring of v.target.footprint) {
          const path = ring.map(ll); path.forEach(extend);
          overlays.push(new lib.Polygon({ map, paths: path, strokeColor: C.target, strokeWeight: 3, fillColor: C.target, fillOpacity: 0.35, zIndex: 2 }));
        }
      } else {
        pin(C.target, "T", `${v.target.name} (point location -- no building outline available)`, ll([v.target.latitude, v.target.longitude]), 5);
        extend(ll([v.target.latitude, v.target.longitude]));
      }
    }

    // Inspection region = the same ring the planner was given.
    if (v.inspection_region) {
      const r = v.inspection_region;
      if (r.path && r.path.length > 2) {
        // Boundary fix: the ring follows the building outline.
        const path = r.path.map(ll); path.forEach(extend);
        overlays.push(new lib.Polygon({ map, paths: path, strokeColor: regionColor, strokeWeight: 2, strokeOpacity: 0.9, fillColor: regionColor, fillOpacity: 0.08, zIndex: 1, clickable: false }));
      } else {
        const circle = new lib.Circle({ map, center: ll(r.center), radius: r.radius_m, strokeColor: regionColor, strokeWeight: 2, strokeOpacity: 0.9, fillColor: regionColor, fillOpacity: 0.10, zIndex: 1, clickable: false });
        overlays.push(circle); const cb = circle.getBounds(); if (cb) { extend(cb.getNorthEast()); extend(cb.getSouthWest()); }
      }
      // Neighbouring buildings the route must avoid: orange = mapped height, white dashed look = height unknown.
      for (const n of (r.neighbours || [])) for (const ring of n.rings)
        overlays.push(new lib.Polygon({ map, paths: ring.map(ll), strokeColor: n.height_m == null ? "#ffffff" : "#ff9800", strokeWeight: 1, strokeOpacity: 0.8, fillColor: "#ff9800", fillOpacity: n.height_m == null ? 0.05 : 0.15, zIndex: 1, clickable: false }));
    }

    // Flown track + completed history (GREEN)
    if (v.drone_track && v.drone_track.length > 1)
      overlays.push(new lib.Polyline({ map, path: v.drone_track.map(ll), strokeColor: C.completed, strokeWeight: 4, strokeOpacity: 0.9, zIndex: 3 }));
    for (const w of (v.completed_history || [])) pin(C.completed, w.sequence, `WP ${w.sequence} (completed)`, ll([w.latitude, w.longitude]), 3);

    // Planned route (BLUE) with direction arrows
    if (v.route && v.route.length > 1) {
      const path = v.route.map(ll); path.forEach(extend);
      overlays.push(new lib.Polyline({ map, path, strokeColor: C.route, strokeWeight: 3, strokeOpacity: 0.95, zIndex: 4,
        icons: [{ icon: { path: google.maps.SymbolPath.FORWARD_OPEN_ARROW, scale: 2.5, strokeColor: C.route }, offset: "50px", repeat: "90px" }] }));
    }

    // Waypoints: exactly the mission's coordinate items
    for (const w of (v.waypoints || [])) {
      const color = w.state === "completed" ? C.completed : (w.corrective || w.state === "current") ? C.replanning : C.route;
      const label = `WP ${w.sequence} ${w.command}${w.altitude_m != null ? " @ " + w.altitude_m + " m" : ""}${w.corrective ? " (RAMR corrective)" : ""} -- ${w.state}`;
      pin(color, w.sequence, label, ll([w.latitude, w.longitude]), w.state === "current" ? 8 : 6);
      extend(ll([w.latitude, w.longitude]));
    }

    if (v.home) pin("#5f6368", "H", "Home / launch", ll(v.home), 2);
    drawDrone(v);

    // Auto-fit only when a NEW plan or a NEW mission version (RAMR replan) arrives.
    if (v.fit_key !== lastFitKey) {
      lastFitKey = v.fit_key;
      const onlyPoint = v.target && !(v.target.footprint && v.target.footprint.length) && !(v.waypoints || []).length && !v.inspection_region;
      if (onlyPoint) { map.setCenter(ll([v.target.latitude, v.target.longitude])); map.setZoom(18); }
      else if (hasBounds) { map.fitBounds(bounds, 48); }
    }
  }

  function drawDrone(v) {
    if (!v.drone) return;
    const el = document.createElement("div"); el.className = "drone";
    if (v.drone.heading_deg != null) el.style.transform = `rotate(${v.drone.heading_deg}deg)`;
    overlays.push(new lib.AdvancedMarkerElement({ map, position: ll([v.drone.latitude, v.drone.longitude]), content: el, title: "Drone", zIndex: 10 }));
  }

  // ---- "click the building once" mode -------------------------------------------
  function drawPick(v) {
    clearOverlays();
    const p = v.pick, bounds = new lib.LatLngBounds();
    if (p.campus_bbox) {
      const [s, n, w, e] = p.campus_bbox;
      const ring = [{lat: s, lng: w}, {lat: s, lng: e}, {lat: n, lng: e}, {lat: n, lng: w}];
      ring.forEach(q => bounds.extend(q));
      overlays.push(new lib.Polygon({ map, paths: ring, strokeColor: "#5f6368", strokeWeight: 2, strokeOpacity: 0.8, fillOpacity: 0, clickable: false, zIndex: 1 }));
    }
    // Real mapped outlines (Microsoft ML or OpenStreetMap) inside the campus, as click targets (hints only).
    for (const ring of (p.footprints || [])) {
      const poly = new lib.Polygon({ map, paths: ring.map(ll), strokeColor: "#ffffff", strokeWeight: 1, strokeOpacity: 0.9, fillColor: "#ffffff", fillOpacity: 0.15, zIndex: 2 });
      poly.addListener && poly.addListener("click", onClick);
      overlays.push(poly);
    }
    if (!clickListenerSet && map.addListener) { map.addListener("click", onClick); clickListenerSet = true; }
    if (p.pick_id !== lastPickFit) {
      lastPickFit = p.pick_id;
      if (p.campus_bbox) map.fitBounds(bounds, 24);
      else if (p.campus_center) { map.setCenter(ll(p.campus_center)); map.setZoom(17); }
    }
  }

  function onClick(ev) {
    const v = current;
    if (!v || !v.pick || v.has_plan || v.pick.state === "locating" || !pickHandler) return;
    const lat = typeof ev.latLng.lat === "function" ? ev.latLng.lat() : ev.latLng.lat;
    const lng = typeof ev.latLng.lng === "function" ? ev.latLng.lng() : ev.latLng.lng;
    if (pickMarker) pickMarker.map = null;
    pickMarker = pin("#F9AB00", "?", "Your pick", { lat, lng }, 9);
    pickHandler(lat, lng);
  }

  function render(v) {
    current = v;
    if (!ready) return;
    if (v && v.has_plan) { if (pickMarker) { pickMarker.map = null; pickMarker = null; } draw(v); }
    else if (v && v.pick) drawPick(v);
    else { clearOverlays(); if (v) drawDrone(v); }
  }

  return {
    init, render,
    isReady: () => ready,
    setPickHandler: fn => { pickHandler = fn; },
    refit: () => { lastFitKey = null; lastPickFit = null; if (current) render(current); },
  };
})();
