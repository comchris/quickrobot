// Color Engine v2 — parametric color system with hull-curve sliders
// Power + Mood sliders -> HSV transformation -> CSS custom properties
// Stored in localStorage (client-side only, no DB)

(function() {
  'use strict';

  // ---- Semantic Palette (base HSL values) ----
  var PALETTE = {
    // Layout surfaces
    'bg-body':     { h: 0,   s: 0,  l: 96 },
    'bg-nav':      { h: 240, s: 18, l: 12 },
    'bg-card':     { h: 240, s: 0,  l: 100 },
    'bg-table-hdr':{ h: 0,   s: 0,  l: 97 },
    'bg-table-row-hover':{ h: 205, s: 65, l: 96 },
    'bg-input':    { h: 0,   s: 0,  l: 100 },
    'bg-select':   { h: 0,   s: 0,  l: 100 },
    // Typography
    'fg-body':     { h: 0,   s: 0,  l: 20 },
    'fg-heading':  { h: 240, s: 18, l: 12 },
    'fg-muted':    { h: 0,   s: 0,  l: 40 },
    'fg-nav-link': { h: 0,   s: 0,  l: 73 },
    'fg-nav-active':{ h: 195, s: 85, l: 64 },
    'fg-nav-chevron':{ h: 0, s: 0,  l: 40 },
    // Interactive
    'link':        { h: 205, s: 65, l: 56 },
    'link-hover':  { h: 205, s: 70, l: 48 },
    'accent':      { h: 195, s: 85, l: 64 },
    'accent-active':{ h: 195, s: 85, l: 64 },
    'nav-brand':   { h: 195, s: 85, l: 64 },
    // Buttons
    'btn-primary': { h: 195, s: 85, l: 64 },
    'btn-danger':  { h: 2,   s: 89, l: 56 },
    'btn-success': { h: 144, s: 54, l: 47 },
    'btn-warning': { h: 30,  s: 80, l: 50 },
    'btn-secondary':{ h: 0,   s: 0,  l: 60 },
    // Status badges
    'badge-running':   { h: 130, s: 50, l: 85 },
    'badge-success':   { h: 144, s: 54, l: 42 },
    'badge-loading':   { h: 210, s: 70, l: 85 },
    'badge-stopped':   { h: 355, s: 55, l: 87 },
    'badge-error':     { h: 45,  s: 75, l: 90 },
    'badge-other':     { h: 0,   s: 0,  l: 89 },
    'badge-system':    { h: 210, s: 70, l: 85 },
    'badge-active':    { h: 130, s: 50, l: 85 },
    'badge-unknown':   { h: 45,  s: 75, l: 90 },
    'badge-failed':    { h: 355, s: 55, l: 87 },
    // Log output (dark backgrounds)
    'log-bg':      { h: 240, s: 18, l: 12 },
    'log-text':    { h: 0,   s: 0,  l: 83 },
    'log-success': { h: 120, s: 54, l: 47 },
    'log-failed':  { h: 2,   s: 89, l: 56 },
    'log-processing':{ h: 30, s: 80, l: 50 },
    'log-received':{ h: 0,   s: 0,  l: 62 },
    // Preview panel (dark terminal)
    'preview-bg':  { h: 240, s: 18, l: 12 },
    'preview-text':{ h: 0,   s: 0,  l: 83 },
    // Form elements
    'form-border': { h: 0,   s: 0,  l: 80 },
    'form-focus':  { h: 195, s: 85, l: 64 },
    'form-label':  { h: 0,   s: 0,  l: 40 },
    // Engine cards
    'card-border': { h: 0,   s: 0,  l: 88 },
    'card-hover-border':{ h: 195, s: 85, l: 64 },
    'card-selected-border':{ h: 195, s: 85, l: 64 },
    'card-selected-bg':{ h: 205, s: 65, l: 96 },
    // Table borders
    'table-border':{ h: 0,   s: 0,  l: 87 },
    'table-row-hover':{ h: 205, s: 65, l: 96 },
    'table-header-bottom':{ h: 0, s: 0, l: 86 },
    // Nav borders
    'nav-border':  { h: 0,   s: 0,  l: 20 },
    'nav-active-border':{ h: 195, s: 85, l: 64 },
    'nav-footer-border':{ h: 0, s: 0, l: 17 },
    // Misc
    'back-link':   { h: 195, s: 85, l: 64 },
    'row-link':    { h: 205, s: 65, l: 56 },
    'orphan-warn': { h: 30,  s: 74, l: 57 },
    // Dev mode badge
    'dev-badge':   { h: 2,   s: 89, l: 56 },
  };

  // ---- Hull Curves (soft limits) ----

  var POWER_CURVES = {
    // Lightness scale: input 0-200 -> multiplier centered at 1.0 (power=100)
    // Power 0 = dim (0.15x), 100 = normal (1.0x), 200 = bright (2.2x)
    scale: function(power) {
      var t = power / 200; // 0.0 -> 1.0
      var eased = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
      return 0.15 + eased * 2.05; // range: 0.15 -> 2.2
    },
    alpha: function(power) {
      var t = power / 200;
      return 0.7 + t * 0.3;
    }
  };

  var MOOD_CURVES = {
    hueShift: function(mood) {
      var t = (mood - 50) / 50;
      return t * 40;
    },
    saturation: function(mood) {
      var t = mood / 100;
      if (t <= 0.6) return 0.3 + (t / 0.6) * 0.7;
      return 1.0 - ((t - 0.6) / 0.4) * 0.15;
    },
    label: function(mood) {
      if (mood <= 25) return 'Cool';
      if (mood <= 75) return 'Balanced';
      return 'Warm';
    }
  };

  // Dark surface names (get alpha dampening)
  var DARK_NAMES = ['log-', 'bg-nav', 'preview-bg'];

  // ---- Disco Mode ----
  // Activation: small grey "pi" symbol in nav footer, toggle on/off
  var disco = {
    active: false,
    rafId: null,
    startTime: 0,
    duration: 4000, // one full spectrum sweep in ms
    startMood: 50
  };

function discoStart(mood) {
       disco.active = true;
       disco.startTime = performance.now();
       disco.duration = 4000;
       try {
         localStorage.setItem('qr_disco_startTime', String(performance.now()));
         localStorage.setItem('qr_disco_duration', '4000');
         localStorage.setItem('qr_disco_active', 'true');
       } catch(ex) {}
       disco.rafId = requestAnimationFrame(function(ts) { discoUpdate(ts); });
     }

 function discoStop() {
       disco.active = false;
       disco.endTimestamp = null;
       if (disco.rafId) { cancelAnimationFrame(disco.rafId); disco.rafId = null; }
       try { localStorage.removeItem('qr_disco_active'); localStorage.removeItem('qr_disco_startTime'); localStorage.removeItem('qr_disco_duration'); } catch(ex) {}
     }

  function discoResume() {
       disco.active = true;
       var savedStart = parseInt(localStorage.getItem('qr_disco_startTime') || String(performance.now()), 10);
       var dur = parseInt(localStorage.getItem('qr_disco_duration') || '4000', 10);
       var elapsed = performance.now() - savedStart;
       disco.startTime = performance.now() - elapsed;
       disco.duration = dur;
       disco.rafId = requestAnimationFrame(function(ts) { discoUpdate(ts); });
       try { localStorage.setItem('qr_disco_active', 'true'); } catch(ex) {}
     }

  function discoGetState() {
       try { return localStorage.getItem('qr_disco_active') === 'true'; } catch(ex) { return false; }
     }

  function discoUpdate(timestamp) {
    if (!disco.active) return;
    var elapsed = timestamp - disco.startTime;
    var t = (elapsed % disco.duration) / disco.duration; // 0-1 cycle
    var mood = 50 + Math.sin(t * Math.PI * 2) * 50; // sweep 0→100→0
    var power = parseInt(document.getElementById('ctrl-power')?.value || '100', 10);
    var colors = ColorEngine.compute(power, mood);
    ColorEngine.apply(colors);
    disco.rafId = requestAnimationFrame(function(ts) { discoUpdate(ts); });
  }

  // ---- Color Engine ----

  var ColorEngine = {
    computing: false,

    compute: function(power, mood) {
      var lightnessMult = POWER_CURVES.scale(power);
      var alpha = POWER_CURVES.alpha(power);
      var hueShift = MOOD_CURVES.hueShift(mood);
      var satFactor = MOOD_CURVES.saturation(mood);

      var colors = {};
      for (var name in PALETTE) {
        if (!PALETTE.hasOwnProperty(name)) continue;
        var base = PALETTE[name];
        var h = (base.h + hueShift + 360) % 360;
        var s = Math.min(100, base.s * satFactor);
        var l = Math.max(5, Math.min(92, base.l * lightnessMult));

        var isDark = false;
        for (var i = 0; i < DARK_NAMES.length; i++) {
          if (name.indexOf(DARK_NAMES[i]) === 0) { isDark = true; break; }
        }

        if (isDark) {
          l = Math.max(8, Math.min(95, l));
          colors[name] = 'hsla(' + h + ', ' + s + '%, ' + l + '%, ' + alpha.toFixed(2) + ')';
        } else {
          colors[name] = 'hsl(' + h + ', ' + s + '%, ' + l + '%)';
        }
      }

  // Dynamic text color inversion based on power
       if (power < 50) {
         var tlight = 90 + lightnessMult * 3;
         colors['fg-body'] = 'hsl(' + h + ', ' + Math.min(40, s) + '%, ' + tlight + '%)';
         colors['fg-muted'] = 'hsl(' + h + ', ' + Math.min(30, s) + '%, ' + (tlight - 12) + '%)';
         // Nav links and chevrons also get inversion treatment
         colors['fg-nav-link'] = 'hsl(' + h + ', ' + Math.min(40, s) + '%, ' + tlight + '%)';
         colors['fg-nav-active'] = 'hsl(' + h + ', ' + Math.min(50, s) + '%, ' + Math.min(80, tlight) + '%)';
         colors['fg-nav-chevron'] = 'hsl(' + h + ', ' + Math.min(25, s) + '%, ' + (tlight - 8) + '%)';
         // Main content links also get inversion treatment
         colors['link'] = 'hsl(' + h + ', ' + Math.min(55, s + 10) + '%, ' + (tlight - 5) + '%)';
         colors['link-hover'] = 'hsl(' + h + ', ' + Math.min(60, s + 15) + '%, ' + Math.min(70, tlight - 10) + '%)';
         colors['row-link'] = 'hsl(' + h + ', ' + Math.min(55, s + 10) + '%, ' + (tlight - 5) + '%)';
       } else if (power > 128) {
        colors['fg-body'] = '#222';
        colors['fg-muted'] = '#555';
        // Nav links and chevrons also get darkened
        colors['fg-nav-link'] = 'rgba(200,200,200,0.7)';
        colors['fg-nav-active'] = 'hsl(205, 65%, 60%)';
        colors['fg-nav-chevron'] = 'rgba(180,180,180,0.5)';
      }

      // Dark link text at high power
      if (power > 128) {
        colors['link'] = 'hsl(205, 65%, 40%)';
        colors['link-hover'] = 'hsl(205, 70%, 32%)';
        colors['row-link'] = 'hsl(205, 65%, 40%)';
      }

      return colors;
    },

    apply: function(colors) {
      var root = document.documentElement;
      for (var name in colors) {
        if (colors.hasOwnProperty(name)) {
          root.style.setProperty('--' + name, colors[name]);
        }
      }
    },

    // Re-apply button colors from CSS variables — called by base.html on slider change
    refreshButtons: function() {
      var btns = document.querySelectorAll('.inst-action');
      for (var i = 0; i < btns.length; i++) {
        var cls = btns[i].className;
        var colorKey = null;
        if (cls.indexOf('btn-primary') !== -1) colorKey = 'btn-primary';
        else if (cls.indexOf('btn-danger') !== -1) colorKey = 'btn-danger';
        else if (cls.indexOf('btn-success') !== -1) colorKey = 'btn-success';
        else if (cls.indexOf('btn-warning') !== -1) colorKey = 'btn-warning';
        else if (cls.indexOf('btn-secondary') !== -1) colorKey = 'btn-secondary';
        if (!colorKey) continue;
        try {
          var bg = getComputedStyle(document.documentElement).getPropertyValue('--' + colorKey).trim();
          btns[i].style.setProperty('background', bg || '', '');
          if (colorKey === 'btn-primary') {
            btns[i].style.setProperty('color', '#1a1a2e', '');
          }
        } catch(e) {}
      }
    },

    generateCSS: function() {
      var rules = [];
      var mappings = [
        ['body', 'background', '--bg-body'],
        ['body', 'color', '--fg-body'],
        ['nav', 'background', '--bg-nav'],
        ['main a', 'color', '--link'],
        ['main h1', 'color', '--fg-heading'],
        ['main h2', 'color', '--fg-body'],
        ['table', 'background', '--bg-card'],
        ['th', 'background', '--bg-table-hdr'],
        ['th', 'color', '--fg-muted'],
        ['td', 'color', '--fg-body'],
        ['tr:hover td', 'background', '--bg-table-row-hover'],
        ['.badge-running', 'background', '--badge-running'],
        ['.badge-success', 'background', '--badge-success'],
        ['.badge-loading', 'background', '--badge-loading'],
        ['.badge-stopped', 'background', '--badge-stopped'],
        ['.badge-error', 'background', '--badge-error'],
        ['.badge-other', 'background', '--badge-other'],
        ['.badge-system', 'background', '--badge-system'],
        ['.badge-active', 'background', '--badge-active'],
        ['.badge-unknown', 'background', '--badge-unknown'],
        ['.badge-failed', 'background', '--badge-failed'],
        ['.detail-card', 'background', '--bg-card'],
        ['.log-output', 'background', '--log-bg'],
        ['.log-output', 'color', '--log-text'],
        ['.log-success', 'color', '--log-success'],
        ['.log-failed', 'color', '--log-failed'],
        ['.log-processing', 'color', '--log-processing'],
        ['.log-received', 'color', '--log-received'],
        ['.ansible-log-deploy', 'color', '--log-success'],
        ['.ansible-log-failed', 'color', '--log-failed'],
        ['.btn-primary', 'background', '--btn-primary'],
        ['.btn-danger', 'background', '--btn-danger'],
        ['.btn-success', 'background', '--btn-success'],
        ['.form-group input, .form-group select', 'background', '--bg-input'],
        ['.form-group label', 'color', '--form-label'],
        ['.form-group input, .form-group select', 'border-color', '--form-border'],
        ['.form-group select:focus, .form-group input:focus', 'border-color', '--form-focus'],
        ['.engine-card', 'border-color', '--card-border'],
        ['.engine-card:hover', 'border-color', '--card-hover-border'],
        ['.engine-card.selected', 'border-color', '--card-selected-border'],
        ['.engine-card.selected', 'background', '--card-selected-bg'],
        ['.engine-card-name', 'color', '--fg-heading'],
        ['.engine-card-desc', 'color', '--fg-muted'],
        ['.preview-panel', 'background', '--preview-bg'],
        ['.preview-panel', 'color', '--preview-text'],
        ['.back-link', 'color', '--back-link'],
        ['.a.row-link', 'color', '--row-link'],
        ['.nav .nav-section-header', 'color', '--fg-muted'],
        ['.nav .nav-section-header:hover', 'color', '--accent'],
        ['.nav .nav-chevron', 'color', '--fg-nav-chevron'],
        ['.nav li a', 'color', '--fg-nav-link'],
        ['.nav li a:hover', 'background', '--bg-nav-hover'],
        ['.nav li a.active', 'background', '--bg-nav-hover'],
        ['.nav li a.active', 'color', '--accent'],
        ['.nav li a.active', 'border-left-color', '--accent-active'],
        ['.nav .nav-footer', 'color', '--fg-muted'],
        ['.nav .nav-footer', 'border-top-color', '--nav-footer-border'],
      ];

      for (var i = 0; i < mappings.length; i++) {
        rules.push(mappings[i][0] + ' {' + mappings[i][1] + ': var(' + mappings[i][2] + ', inherit); }');
      }

      rules.push('.btn-primary { color: var(--fg-heading, #1a1a2e); }');
      rules.push('.btn-danger { color: #fff; }');
      rules.push('.btn-success { color: #fff; }');
      rules.push('.nav li a:hover, .nav li a.active { background: var(--bg-nav-hover); }');

      return rules.join('\n  ');
    },

    generateSliderCSS: function() {
       return [
         '/* Color Engine v2 -- Slider UI */',
         '.color-controls { padding: 6px 12px; font-size: 0.75em; }',
         '.slider-label { display: flex; align-items: center; gap: 4px; margin-bottom: 6px; color: var(--fg-muted, #666); }',
         '.slider-label input[type="range"] { flex: 1; margin: 2px 0; cursor: pointer; }',
         '.slider-label output { min-width: 48px; text-align: right; font-size: 70%; color: var(--fg-muted, #888); font-family: monospace; white-space: nowrap; display: none; }',
         '.color-footer { display: flex; align-items: center; justify-content: space-between; margin-top: 4px; }',
         '.nav-pi { cursor: pointer; color: var(--fg-muted, #888); font-size: 0.5em; user-select: none; opacity: 0.4; line-height: 1; }',
         '.nav-pi:hover { opacity: 1; }',
         '.nav-pi.disco-active { opacity: 1; color: var(--accent, #4fc3f7); }',
         '.ctrl-reset { cursor: pointer; color: var(--accent, #4fc3f7); font-size: 60%; user-select: none; padding: 2px 8px; }',
         '.ctrl-reset:hover { opacity: 0.7; text-decoration: underline; }',
         '.nav-footer .disco-pi { cursor: pointer; color: var(--fg-muted, #888); font-size: 0.45em; user-select: none; opacity: 0.5; margin-left: 8px; vertical-align: middle; }',
         '.nav-footer .disco-pi:hover { opacity: 1; }',
         '.nav-footer .disco-pi.disco-active { opacity: 1; color: var(--accent, #4fc3f7); }',
       ].join('\n');
     },

    buildNavSection: function(power, mood) {
       return [
    '<li class="nav-section" data-section="skins">',
          '  <div class="nav-section-header">Skins <span class="nav-chevron">&#9660;</span></div>',
          '  <ul class="nav-section-items">',
          '    <li class="color-controls">',
          '      <label class="slider-label ctrl-power">',
          '        <input type="range" id="ctrl-power" min="1" max="200" value="' + power + '" step="1">',
          '        <output id="out-power" for="ctrl-power"></output>',
          '      </label>',
          '      <label class="slider-label ctrl-mood">',
          '        <input type="range" id="ctrl-mood" min="1" max="100" value="' + mood + '" step="1">',
          '        <output id="out-mood" for="ctrl-mood"></output>',
          '      </label>',
          '      <div class="color-footer">',
          '        <span id="disco-pi" class="disco-pi nav-pi">&#960;</span>',
          '        <span class="ctrl-reset" id="btn-reset-colors">noskin</span>',
          '      </div>',
          '    </li>',
          '  </ul>',
        '</li>'
      ].join('\n');
    },

    injectNavSection: function(html) {
      // Sliders are now in base.html — only run if not found (backward compat)
      if (document.getElementById('ctrl-power')) return;

      var navUl = document.querySelector('nav > ul');
      if (!navUl) return;

      var isFirstVisit = localStorage.getItem('color_skins_hidden') !== 'true';
       if (isFirstVisit) {
         localStorage.setItem('color_skins_hidden', 'true');
       }

      var themesLi = document.createElement('li');
      themesLi.className = 'nav-section';
      themesLi.setAttribute('data-section', 'themes');
      themesLi.innerHTML = [
      '<div class="nav-section-header">Skins <span class="nav-chevron">&#9660;</span></div>',
         '<ul class="nav-section-items">',
         '  <li class="color-controls">',
         '    <label class="slider-label ctrl-power">',
         '      <input type="range" id="ctrl-power" min="1" max="200" value="100" step="1">',
         '      <output id="out-power" for="ctrl-power"></output>',
         '    </label>',
         '    <label class="slider-label ctrl-mood">',
         '      <input type="range" id="ctrl-mood" min="1" max="100" value="50" step="1">',
         '      <output id="out-mood" for="ctrl-mood"></output>',
         '    </label>',
         '    <div class="color-footer">',
         '      <span id="disco-pi" class="disco-pi nav-pi">&#960;</span>',
         '      <span class="ctrl-reset" id="btn-reset-colors">noskin</span>',
         '    </div>',
         '  </li>',
        '</ul>',
        '</li>'
      ].join('\n');

      navUl.appendChild(themesLi);

      var themesSection = document.querySelector('[data-section="themes"]');
      if (themesSection) {
        // Restore collapse state from localStorage
      var savedNavState = localStorage.getItem('qr_nav_skins');
         if (isFirstVisit || savedNavState === 'true') {
          themesSection.setAttribute('data-collapsed', 'true');
          var itemsEl = themesSection.querySelector('.nav-section-items');
          var chevronEl = themesSection.querySelector('.nav-chevron');
          if (itemsEl) itemsEl.style.display = 'none';
          if (chevronEl) chevronEl.innerHTML = '&#9654;';
        }

        var headerEl = themesSection.querySelector('.nav-section-header');
        if (headerEl) {
          headerEl.addEventListener('click', function(e) {
            e.stopPropagation();
            var isCollapsed = themesSection.getAttribute('data-collapsed') === 'true';
            themesSection.setAttribute('data-collapsed', isCollapsed ? 'false' : 'true');
            var itemsEl = themesSection.querySelector('.nav-section-items');
            var chevronEl = themesSection.querySelector('.nav-chevron');
            if (isCollapsed) {
              if (itemsEl) itemsEl.style.display = 'block';
              if (chevronEl) chevronEl.innerHTML = '&#9660;';
            } else {
              if (itemsEl) itemsEl.style.display = 'none';
              if (chevronEl) chevronEl.innerHTML = '&#9654;';
            }
            try { localStorage.setItem('qr_nav_skins', themesSection.getAttribute('data-collapsed')); } catch(ex) {}
          });
        }
      }
    },

    wireSliders: function() {
      var self = this;
      var powerSlider = document.getElementById('ctrl-power');
      var moodSlider = document.getElementById('ctrl-mood');
      var outPower = document.getElementById('out-power');
      var outMood = document.getElementById('out-mood');

      // rAF-based throttle for smooth updates
      var rafPending = false;
      var currentP = 100, currentM = 50;

      function scheduleUpdate() {
        if (rafPending) return;
        rafPending = true;
        requestAnimationFrame(function() {
          rafPending = false;
          var colors = self.compute(currentP, currentM);
          self.apply(colors);
          self.refreshButtons(); // update dynamic action button colors
          self.save(currentP, currentM);
          updateDisplay();
        });
      }

      function updateDisplay() {
        var p = parseInt(powerSlider.value, 10);
        var m = parseInt(moodSlider.value, 10);
        if (outPower) outPower.textContent = Math.round(POWER_CURVES.scale(p) * 100) + '%';
        // Mood label removed — slider value implicit
      }

      function onSliderInput() {
        currentP = parseInt(powerSlider.value, 10);
        currentM = parseInt(moodSlider.value, 10);

        // Exit disco if slider moved during disco
        if (disco.active) discoStop();

        scheduleUpdate();
      }

      if (powerSlider) {
        powerSlider.addEventListener('input', onSliderInput);
      }
      if (moodSlider) {
        moodSlider.addEventListener('input', onSliderInput);
      }

     // Disco mode: pi symbol in nav toggles it (only wire if base.html didn't already)
        var discoPi = document.getElementById('disco-pi');
        if (discoPi && !window._qr_color_engine_loaded) {
          discoPi.title = 'Disco mode';
          discoPi.addEventListener('click', function() {
            var p = parseInt(powerSlider ? powerSlider.value : 100, 10);
            var m = parseInt(moodSlider ? moodSlider.value : 50, 10);

            if (disco.active) {
              discoStop();
              var colors = self.compute(p, m);
              self.apply(colors);
              self.save(p, m);
              discoPi.classList.remove('disco-active');
            } else {
              discoStart(m);
              discoPi.classList.add('disco-active');
           }
         });
       }

      // Reset button
        var resetBtn = document.getElementById('btn-reset-colors');
        if (resetBtn && !window._qr_color_engine_loaded) {
         resetBtn.addEventListener('click', function() {
          discoStop();
          var defaults = { power: 100, mood: 50 };
          var colors = self.compute(defaults.power, defaults.mood);
          self.apply(colors);
          self.save(defaults.power, defaults.mood);
          if (powerSlider) powerSlider.value = defaults.power;
          if (moodSlider) moodSlider.value = defaults.mood;
          updateDisplay();
        });
      }

      // Initial display update
      updateDisplay();
    },

    // ---- Storage ----

    save: function(power, mood) {
      try {
        localStorage.setItem('qr_color_engine', JSON.stringify({ power: power, mood: mood }));
      } catch (e) { /* quota exceeded or disabled */ }
    },

    load: function() {
      try {
        var saved = localStorage.getItem('qr_color_engine');
        if (saved) {
          var parsed = JSON.parse(saved);
          if (parsed.power != null && parsed.mood != null) {
            return { power: Math.max(0, Math.min(200, parsed.power)), mood: Math.max(0, Math.min(100, parsed.mood)) };
          }
        }
      } catch (e) { /* corrupt data */ }
      return { power: 100, mood: 50 }; // defaults (power=100 = normal)
    },

    // ---- Boot ----

     init: function() {
        // First pass: save current slider DOM values to localStorage (captures any pending user change)
        var powerSlider = document.getElementById('ctrl-power');
        var moodSlider = document.getElementById('ctrl-mood');
        if (powerSlider && moodSlider) {
          this.save(parseInt(powerSlider.value, 10), parseInt(moodSlider.value, 10));
        }
        // Now load from localStorage (will reflect saved DOM values, not stale stored values)
        var settings = this.load();
        var colors = this.compute(settings.power, settings.mood);
        this.apply(colors);
        this.injectNavSection();
        // Set slider values to match loaded settings before wiring events
        if (powerSlider) powerSlider.value = settings.power;
        if (moodSlider) moodSlider.value = settings.mood;
        this.wireSliders();
        // Restore disco state across page navigation
         if (discoGetState()) {
           discoResume();
           var pi = document.getElementById('disco-pi');
           if (pi) pi.classList.add('disco-active');
         }
     },

      // Disco mode controls — exposed for base.html lazy-load disco toggle
      discoStart: function(m) { discoStart(m); },
      discoStop: function() { discoStop(); },
      discoGetState: function() { return discoGetState(); },
   };

   // Make available globally for inline JS that needs it
  window.ColorEngine = ColorEngine;

  // Boot: load settings, compute colors, inject nav section, wire sliders
  ColorEngine.init();
})();
