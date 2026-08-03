"""Wizard step navigation structural validation for instances_new.html.

Tests that the Create Instance wizard HTML/JS has correct structure for
6-step navigation: proper elements, button IDs, event handlers, and state
variables. Uses static file analysis — no browser needed.

26 tests, ~0.15s. Auto-marked with @pytest.mark.part_b via conftest.py.

Run this file alone:
  pytest tests/test_wizard_steps.py -v --tb=line

Run as part of the full suite split (avoid shell timeout on large output):
  Part A (core API + config, 99 tests):   pytest tests/ -m part_a
  Part B (integration + UI + infra, 86 tests): pytest tests/ -m part_b
"""

import re
import jinja2
from pathlib import Path
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEBUI_DIR = PROJECT_ROOT / "webui"
WIZARD_HTML = PROJECT_ROOT / "webui" / "instances_new.html"


# ---------------------------------------------------------------------------
# Local fixtures — template_env not shared from test_webui.py
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def template_env():
    """Jinja2 environment with FileSystemLoader pointing to webui/."""
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(WEBUI_DIR)),
        autoescape=False,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_wizard():
    """Read wizard HTML once, cached for all tests in this module."""
    return WIZARD_HTML.read_text()


# ---------------------------------------------------------------------------
# HTML structure tests — verify all 6 steps exist with correct attributes
# ---------------------------------------------------------------------------

class TestWizardHtmlStructure:
    """Verify HTML elements required for step navigation."""

    def setup_method(self):
        self.html = _read_wizard()

    def test_all_6_steps_exist(self):
        """Each of the 6 wizard steps must have a <div class='wizard-step'> with data-step."""
        # class= may include extra classes (e.g. "wizard-step active") — use flexible match
        step_divs = re.findall(r'class="[^"]*wizard-step[^"]*"[^>]*data-step="(\d+)"', self.html)
        assert len(step_divs) == 6, f"Expected 6 step divs, found {len(step_divs)}: {step_divs}"
        assert set(step_divs) == {"1", "2", "3", "4", "5", "6"}, \
            f"Step indices mismatch: got {sorted(step_divs)}, expected ['1','2','3','4','5','6']"

    def test_step_indicators_exist(self):
        """Wizard progress bar must have 6 indicator divs with data-wstep attributes."""
        indicators = re.findall(r'class="[^"]*wizard-step-indicator[^"]*"[^>]*data-wstep="(\d+)"', self.html)
        assert len(indicators) == 6, f"Expected 6 step indicators, found {len(indicators)}: {indicators}"
        assert set(indicators) == {"1", "2", "3", "4", "5", "6"}, \
            f"Indicator indices mismatch: got {sorted(indicators)}"

    def test_navigation_buttons_exist(self):
        """Wizard must have prev-btn, next-btn, and create-btn elements."""
        assert re.search(r'id="prev-btn"', self.html), "Missing #prev-btn element"
        assert re.search(r'id="next-btn"', self.html), "Missing #next-btn element"
        assert re.search(r'id="create-btn"', self.html), "Missing #create-btn element"

    def test_step_visibility_classes(self):
        """Wizard steps use .wizard-step + .active CSS class for visibility toggling."""
        # The CSS defines: .wizard-step { display:none; } and .wizard-step.active { display:block; }
        assert 'class="wizard-step"' in self.html, "Missing wizard-step class"
        assert '.wizard-step.active' in self.html or \
               re.search(r'\.wizard-step\.active', self.html) or \
               'wizard-step.active' in self.html, \
            "Missing .wizard-step.active visibility rule"

    def test_button_visibility_css(self):
        """Step 1: prev hidden, next visible. Step 6: create visible, next hidden."""
        # Verify CSS rules control button display (JS toggles style.display)
        assert 'display:none' in self.html, "Missing CSS display:none for initial state"

    def test_hidden_engine_select(self):
        """Step 2: engine-select must exist as hidden <select> for JS sync."""
        assert re.search(r'id="engine-select"', self.html), "Missing #engine-select (BUG-1 fix)"

    def test_step_content_ids(self):
        """Each step has a unique content container ID for JS reference."""
        expected_containers = [
            'node-card-grid',    # Step 1: node selection
            'engine-card-grid',  # Step 2: engine selection
            'template-results',  # Step 3: binary templates
            'preset-results',    # Step 4: presets
            'inst-name',         # Step 5: instance name input
        ]
        for cid in expected_containers:
            assert re.search(r'id="' + re.escape(cid) + r'"', self.html), \
                f"Missing container #{cid} for step navigation content"


# ---------------------------------------------------------------------------
# JS structure tests — verify event handlers and state management
# ---------------------------------------------------------------------------

class TestWizardJsStructure:
    """Verify JavaScript structure for step navigation."""

    def setup_method(self):
        self.html = _read_wizard()
        # Extract all <script> blocks
        self.scripts = re.findall(r'<script>(.*?)</script>', self.html, re.DOTALL)
        assert len(self.scripts) >= 1, "No script blocks found in wizard"
        self.wizard_js = self.scripts[0]

    def test_go_to_step_function_exists(self):
        """Must have goToStep(step) function for step transitions."""
        assert 'function goToStep(step)' in self.wizard_js or \
               re.search(r'function\s+goToStep\s*\(', self.wizard_js), \
            "Missing goToStep() function"

    def test_prev_button_click_handler(self):
        """#prev-btn must have click handler that calls goToStep(currentStep - 1)."""
        assert re.search(r'getElementById\(\s*["\x27]prev-btn["\x27]\s*\)', self.wizard_js), \
            "Missing getElementById('#prev-btn')"
        assert re.search(r'addEventListener\s*\(\s*["\x27]click["\x27]', self.wizard_js) and \
               'goToStep' in self.wizard_js, \
            "Missing click event listener with goToStep call"

    def test_next_button_click_handler(self):
        """#next-btn must have click handler with step validation guards."""
        next_id = re.search(r'getElementById\(\s*["\x27]next-btn["\x27]\s*\)', self.wizard_js)
        assert next_id, "Missing getElementById('#next-btn')"

    def test_create_button_click_handler(self):
        """#create-btn must have click handler for instance creation."""
        create_id = re.search(r'getElementById\(\s*["\x27]create-btn["\x27]\s*\)', self.wizard_js)
        assert create_id, "Missing getElementById('#create-btn')"

    def test_step_indicator_click_handlers(self):
        """Step indicators (.wizard-step-indicator) must have click handlers."""
        assert re.search(r'querySelectorAll\s*\(\s*["\x27]\.wizard-step-indicator["\x27]', self.wizard_js), \
            "Missing querySelectorAll('.wizard-step-indicator')"

    def test_validation_guards_on_next(self):
        """Next button must validate: step 1 needs node selection, step 2 needs engine."""
        # Check for error status messages in Next handler
        assert re.search(r"Select a target node", self.wizard_js), \
            "Missing validation guard: 'Select a target node'"
        assert re.search(r"Select an engine type", self.wizard_js) or \
               re.search(r"Select an engine", self.wizard_js), \
            "Missing validation guard: 'Select an engine type'"

    def test_state_variables_preserved(self):
        """Must have state variables for selected node, engine, and current step."""
        assert 'selectedNodeId' in self.wizard_js, "Missing selectedNodeId state"
        assert 'selectedEngineId' in self.wizard_js, "Missing selectedEngineId state"
        assert 'currentStep' in self.wizard_js, "Missing currentStep variable"

    def test_button_visibility_logic(self):
        """goToStep must toggle button visibility: prev hidden at step 1, create at step 6."""
        # Check for conditional display logic based on step number
        assert re.search(r'step\s*>\s*1', self.wizard_js) or \
               re.search(r'currentStep\s*>\s*1', self.wizard_js), \
            "Missing prev button visibility check (step > 1)"
        assert re.search(r'step\s*===\s*6', self.wizard_js) or \
               re.search(r'currentStep\s*===\s*6', self.wizard_js), \
            "Missing step 6 create button logic"


# ---------------------------------------------------------------------------
# Data binding tests — verify API calls and data loading
# ---------------------------------------------------------------------------

class TestWizardDataBinding:
    """Verify the wizard makes correct API calls for dynamic content."""

    def setup_method(self):
        self.html = _read_wizard()
        self.scripts = re.findall(r'<script>(.*?)</script>', self.html, re.DOTALL)
        self.wizard_js = self.scripts[0]

    def test_load_nodes_on_init(self):
        """Wizard must call loadNodes() on initialization."""
        assert 'loadNodes()' in self.wizard_js or \
               'loadNodes();' in self.wizard_js, \
            "Missing loadNodes() call on init"

    def test_load_engines_on_init(self):
        """Wizard must call loadEngines() on initialization."""
        assert 'loadEngines()' in self.wizard_js or \
               'loadEngines();' in self.wizard_js, \
            "Missing loadEngines() call on init"

    def test_api_call_patterns(self):
        """Wizard must use qrApi() for API calls to nodes, engines, presets endpoints."""
        assert re.search(r'qrApi\s*\(\s*["\x27]/nodes["\x27]', self.wizard_js) or \
               'nodes_raw' in self.wizard_js, \
            "Missing API call for node data"
        assert re.search(r'qrApi\s*\(\s*["\x27]/engine/', self.wizard_js), \
            "Missing API call for engine/preset data"

    def test_json_data_injection(self):
        """Server must inject JSON data via template variables (nodes_raw, engines_raw)."""
        assert '{{ nodes_raw | safe }}' in self.html or \
               '{{ nodes_raw }}' in self.html, \
            "Missing nodes_raw template variable"
        assert '{{ engines_raw | safe }}' in self.html or \
               '{{ engines_raw }}' in self.html, \
            "Missing engines_raw template variable"


# ---------------------------------------------------------------------------
# CSS structure tests — verify styling classes exist
# ---------------------------------------------------------------------------

class TestWizardCssStructure:
    """Verify CSS rules for wizard layout and step visibility."""

    def setup_method(self):
        self.html = _read_wizard()

    def test_wizard_progress_css(self):
        """Progress bar must have .wizard-progress class with flexbox layout."""
        assert '.wizard-progress' in self.html, "Missing .wizard-progress CSS rule"

    def test_wiz_card_selected_state(self):
        """Cards must have .wiz-card.selected state for selection highlighting."""
        assert '.wiz-card.selected' in self.html or \
               re.search(r'\.wiz-card\.selected', self.html), \
            "Missing .wiz-card.selected CSS rule"

    def test_step_indicator_states(self):
        """Step indicators must have .active and .done states."""
        assert '.wizard-step-indicator.active' in self.html or \
               re.search(r'\.wizard-step-indicator\.active', self.html), \
            "Missing .wizard-step-indicator.active CSS rule"
        assert '.wizard-step-indicator.done' in self.html or \
               re.search(r'\.wizard-step-indicator\.done', self.html), \
            "Missing .wizard-step-indicator.done CSS rule"

    def test_step_visibility_css(self):
        """Steps must be hidden by default, shown via .active class."""
        assert '.wizard-step' in self.html and 'display:none' in self.html, \
            "Missing .wizard-step { display: none } base rule"


# ---------------------------------------------------------------------------
# Integration test — template compilation
# ---------------------------------------------------------------------------

def test_wizard_template_compiles(template_env):
    """The wizard template must compile without Jinja2 errors."""
    t = template_env.get_template("instances_new.html")
    assert t is not None


def test_no_unclosed_if_in_wizard():
    """Wizard must not have unclosed {% if %} blocks."""
    content = WIZARD_HTML.read_text()
    opens = len(re.findall(r'\{%\s+if\b', content))
    closes = len(re.findall(r'\{%\s+endif\s*%?\}', content))
    assert opens == closes, \
        f"instances_new.html: {opens} 'if' blocks but only {closes} 'endif'. Missing {opens - closes}."


def test_no_unclosed_for_in_wizard():
    """Wizard must not have unclosed {% for %} blocks."""
    content = WIZARD_HTML.read_text()
    opens = len(re.findall(r'\{%\s+for\b', content))
    closes = len(re.findall(r'\{%\s+endfor\s*%?\}', content))
    assert opens == closes, \
        f"instances_new.html: {opens} 'for' blocks but only {closes} 'endfor'. Missing {opens - closes}."
