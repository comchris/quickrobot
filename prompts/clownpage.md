# Single-Shot Clown Rental Webpage Generator

## ⚡ CONSTRAINTS (READ FIRST)

You are a web development agent producing a complete, self-contained webpage in one shot.
No tool calls after this prompt — everything must be delivered in your single response.

### Workspace
- Create output directory: `mkdir -p ./clown_rental_TIMESTAMP` where TIMESTAMP is current date+time (e.g., `20260718_220000`)
- If the user provided a different target path, use it instead of `./`
- Write ALL files into this single directory only.

### Deliverable
One complete HTML file: `index.html` — must include everything inline:
  - HTML5 structure with valid DOCTYPE
  - CSS inside `<style>` tags (no external stylesheet)
  - JavaScript inside `<script>` tags (no external JS library)
  - No external dependencies (no CDN links, no Google Fonts, no image URLs)
  - No framework usage (no Tailwind, Bootstrap, etc.)

### Visual Requirements
- Dynamic animated background using pure CSS (keyframes, gradients, pseudo-elements) — must create a lively circus/clown atmosphere
- All graphics via inline SVG or CSS shapes — no `<img>` tags with external sources
- Professional design: cohesive color palette, readable typography (system fonts only), responsive layout
- Balance visual impact with performance — no excessive animations that hurt usability

### Content Sections (ALL REQUIRED)
1. **Hero** — Eye-catching header with business name, tagline, animated background
2. **Services** — 3-4 service cards with icons (inline SVG)
3. **Pricing** — Pricing table or tiers (HTML `<table>` or flexbox grid)
4. **Gallery** — CSS-shape or SVG illustration placeholders (no external images)
5. **Contact** — Contact form layout + fictional contact details

### Fictional Business Details
- Name: "Clown Verleih MusterGamma"
- Address: `Musterstraße 123, 10115 Berlin`
- Phone: `+49 (0)30 12345678`
- Email: `info@clown-verleih-mustergamma.de`
- Social: Instagram & Facebook handles
- Language: German visible content, English code comments

### Output Format
At the end of your response, include a verification block:
```
### ✅ VERIFICATION CHECKLIST
- [ ] Directory created: clown_rental_TIMESTAMP/
- [ ] index.html: valid HTML5, inline CSS+JS, no external deps
- [ ] Hero section with animated background
- [ ] Services section (3-4 cards)
- [ ] Pricing section
- [ ] Gallery section (SVG/CSS shapes only)
- [ ] Contact section
- [ ] All content in German, code comments in English
- [ ] Responsive design (media queries present)
- [ ] System fonts used (no @import fonts)
```

### DO NOT
- Install any packages, tools, or frameworks
- Change directory or create subdirectories outside the target folder
- Start a webserver (no `python -m http.server`, no `npx serve`)
- Open a browser or use headless browsers
- Fetch any external resources (fonts, images, CSS, JS)
- Use placeholder image URLs (e.g., via.placeholder.com, picsum.photos)
- Make network calls or API requests

---

**Begin.** Produce the complete `index.html` file content below.