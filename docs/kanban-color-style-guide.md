# Kanban Dashboard Color Style Guide

Status: Draft
Scope: `/home/david/kanban-browser/static/style.css` and related dashboard UI

## 1. Goal

Make the kanban dashboard feel calm, readable, and trustworthy. The interface should prioritize legibility first, then hierarchy, then visual polish. The current palette is too dark, too noisy, and relies on low-contrast gray text; this guide replaces that with a cleaner dashboard palette.

## 2. Design direction

Use a modern product-dashboard aesthetic inspired by Linear / Stripe / Notion-style clarity:

- Neutral, slightly cool background
- White or near-white surfaces for primary reading areas
- Strong but restrained accent color for focus and actions
- Status colors that are vivid but not neon
- Soft borders and subtle elevation instead of heavy outlines
- Minimal use of pure black; avoid black text on dark surfaces entirely

The main mental model is:

- Backgrounds recede
- Cards and controls lift slightly
- Text is dark on light surfaces
- Accents are reserved for meaning, not decoration

## 3. Core color palette

### Neutral surfaces

- Page background: `#f4f7fb`
- App shell / subtle panel background: `#eef2f7`
- Card / surface: `#ffffff`
- Elevated surface: `#f8fafc`
- Strong hover surface: `#e9eef5`

### Borders and dividers

- Default border: `#d7deea`
- Subtle divider: `#e5ebf2`
- Strong focus border: `#5b7cff`

### Text

- Primary text: `#18212f`
- Secondary text: `#526071`
- Muted text / helper text: `#7b8796`
- Disabled text: `#a5afbd`

### Accents

- Primary blue / interactive: `#4667f2`
- Hover blue: `#5b7cff`
- Active blue: `#3554d6`
- Link blue: `#3858e9`

### Status colors

Use these only for semantic meaning:

- Todo / neutral: `#7b8796`
- Ready / amber: `#c58b1b`
- Running / blue: `#4667f2`
- Done / green: `#1f9d55`
- Blocked / red: `#d64545`

### Soft status backgrounds

Use very pale tints behind badges or cards:

- Blue tint: `rgba(70, 103, 242, 0.10)`
- Green tint: `rgba(31, 157, 85, 0.10)`
- Amber tint: `rgba(197, 139, 27, 0.12)`
- Red tint: `rgba(214, 69, 69, 0.10)`
- Neutral tint: `rgba(123, 135, 150, 0.10)`

## 4. Typography rules

### Text hierarchy

- Headings: `#18212f`, semibold
- Body text: `#243041`
- Supporting metadata: `#526071`
- Small labels: `#7b8796`

### Readability rules

- Never use pure black text on dark backgrounds
- Never use low-contrast gray text for primary content
- Reserve muted text for supporting labels only
- For any text smaller than 12px, contrast must be clearly readable at a glance

## 5. Component rules

### Page shell

- Use the light page background as the default canvas
- Keep the header slightly elevated from the background
- Use soft bottom borders or shadow for separation

### Cards

- Cards should be white surfaces with subtle borders
- Card titles should use strong dark text
- Secondary lines should use muted text
- Avoid dark card interiors unless the whole app is intentionally dark, which this guide does not recommend

### Tables

- Table headers: uppercase or small-caps feel, but keep them readable
- Row hover: very light blue-gray highlight, not dark gray
- Status cells should be colored by meaning, but still readable in black/dark text

### Kanban columns

- Column headers should sit on light surfaces
- Column bodies should remain bright and open
- Use tinted headers or badges to signal status, not full-saturation blocks
- Card previews should remain subtle and never compete with task titles

### Buttons

- Primary button: solid blue with white text
- Secondary button: white background, gray border, dark text
- Danger button: red outline or red tint, not aggressive solid red by default
- Hover states should be obvious but not flashy

### Inputs and selects

- White background for form controls
- Dark text by default
- Clear focus ring in blue
- Placeholder text should be muted, not gray-on-gray

### Modals / overlays

- Overlay should use a soft translucent dark layer
- Modal content should be white or near-white
- Modal text should match the main body text scale and contrast

### Chips / pills / badges

- Use small color-tinted backgrounds with strong text
- Avoid large saturated fills
- Badges should read as status labels, not decorative blobs

## 6. Interaction states

Use this order of emphasis:

1. Default: subtle and calm
2. Hover: slightly darker or slightly brighter surface
3. Focus: visible blue ring
4. Active: stronger tint or border
5. Disabled: desaturated and lower opacity

Rules:

- Hover should never reduce contrast
- Focus should always be obvious
- Active states should be distinct even for users with imperfect vision
- Do not rely on color alone for critical interaction feedback; combine color with shape, border, or weight

## 7. Accessibility targets

Recommended minimums:

- Body text: WCAG AA or better
- Small helper text: at least AA and preferably AAA when possible
- Buttons and controls: strong contrast with clear hover/focus states
- Status colors: never the only signal; pair with label text

Practical rule:

- If a label is 12px or smaller, it should be one of the darker readable neutrals, not a washed-out gray
- If a surface is dark, text on it must be light enough to read instantly
- If a surface is light, text on it should be dark enough to feel crisp

## 8. Recommended dashboard palette application

### Use light mode as the default

The dashboard is a working tool, not a cinematic app. Light mode gives better scanability for long task lists and reduces the risk of black-on-dark rendering bugs.

### Suggested mapping by region

- App background: `#f4f7fb`
- Top header: `#ffffff`
- Main cards: `#ffffff`
- Side panels / drawers: `#ffffff` or `#f8fafc`
- Kanban columns: `#ffffff`
- Selected row / active filter: pale blue tint
- Session panel / modals: white surface with soft shadow
- Search bar / selects: white backgrounds with gray borders

## 9. What to avoid

- Pure black backgrounds (`#000000`) except maybe tiny nested code samples
- Pure black text on any non-white background
- Dense dark chrome everywhere
- Low-contrast gray-on-gray UI
- Neon saturated status blocks
- Too many border colors
- More than one accent color competing for primary actions

## 10. Implementation plan

1. Set a global light base on `body`.
2. Convert all major containers to white or near-white surfaces.
3. Standardize text colors into primary / secondary / muted tiers.
4. Replace current dark control styles with light control styles.
5. Rework status colors into semantic badges and light tints.
6. Verify every input, table cell, modal, and panel has explicit readable text color.
7. Test the page in both table and board views.
8. Check the mobile layout for any remaining dark elements or low-contrast text.

## 11. Verification checklist

- No black text appears on dark surfaces
- Primary text is immediately readable everywhere
- Secondary text is soft but still legible
- Status colors look intentional, not neon
- Buttons and chips are visually grouped and easy to scan
- Focus rings are obvious
- Mobile and desktop look like the same system

## 12. Follow-up note for implementation

When applying this guide, prefer changing the full palette in one pass instead of patching individual low-contrast elements piecemeal. The current dashboard looks bad mostly because the palette is internally inconsistent, not because of one or two bad colors.
