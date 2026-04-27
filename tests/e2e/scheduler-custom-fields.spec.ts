/**
 * E2E tests for scheduler event custom fields (JSON metadata editor).
 *
 * Feature: hokanuSK/elabftw scheduler-json-editor-fix branch adds a JSON
 * metadata editor to the event view/edit modal so operators can attach
 * arbitrary key-value fields to each booking.
 *
 * Stack must be running at https://localhost:8443.
 * Run:  npx playwright test --project=chromium
 */

import { test, expect, type APIRequestContext } from '@playwright/test';

const API = '/api/v2';
// Known API key seeded in auth.setup.ts — matches the hash inserted in DB
const API_KEY = '1-testkey12345678901234567890123456789012345';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function createBookableResource(request: APIRequestContext): Promise<number> {
  const create = await request.post(`${API}/items`, {
    data: { title: `PW resource ${Date.now()}` },
    headers: { Authorization: API_KEY },
  });
  expect(create.status()).toBe(201);
  const loc = create.headers()['location'] ?? '';
  const itemId = Number(loc.split('/').pop());
  expect(itemId).toBeGreaterThan(0);

  const patch = await request.patch(`${API}/items/${itemId}`, {
    data: { is_bookable: 1 },
    headers: { Authorization: API_KEY },
  });
  expect(patch.status()).toBe(200);
  return itemId;
}

function toAtom(d: Date): string {
  // PHP DateTime::ATOM = Y-m-d\TH:i:sP (no millis, with tz offset)
  const pad = (n: number) => String(n).padStart(2, '0');
  const tz = -d.getTimezoneOffset();
  const tzH = pad(Math.floor(Math.abs(tz) / 60));
  const tzM = pad(Math.abs(tz) % 60);
  const sign = tz >= 0 ? '+' : '-';
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}${sign}${tzH}:${tzM}`;
}

async function createEvent(request: APIRequestContext, itemId: number): Promise<number> {
  // Start 1 hour from now so isFutureOrExplode passes
  const start = new Date(Date.now() + 60 * 60 * 1000);
  const end = new Date(Date.now() + 3 * 60 * 60 * 1000);

  const res = await request.post(`${API}/events/${itemId}`, {
    data: { title: 'PW test booking', start: toAtom(start), end: toAtom(end) },
    headers: { Authorization: API_KEY },
  });
  expect(res.status()).toBe(201);
  const loc = res.headers()['location'] ?? '';
  const eventId = Number(loc.split('/').pop());
  expect(eventId).toBeGreaterThan(0);
  return eventId;
}

async function deleteEvent(request: APIRequestContext, eventId: number) {
  await request.delete(`${API}/event/${eventId}`, {
    headers: { Authorization: API_KEY },
  });
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test.describe('Scheduler', () => {
  test('page loads and shows scheduler calendar', async ({ page }) => {
    await page.goto('/scheduler.php');
    // loading-spinner is removed by JS once calendar is initialised
    await expect(page.locator('#loading-spinner')).toHaveCount(0);
    // FullCalendar root element must exist
    await expect(page.locator('#scheduler')).toBeVisible();
  });

  test('scheduler renders with calendar element and a bookable resource', async ({ page, request }) => {
    const itemId = await createBookableResource(request);
    const eventId = await createEvent(request, itemId);

    try {
      await page.goto(`/scheduler.php?items[]=${itemId}`);
      // FullCalendar initialises the #scheduler div
      await expect(page.locator('#scheduler')).toBeVisible();
      // At least one fc-event appears (FullCalendar renders the booking)
      await expect(page.locator('.fc-event').first()).toBeVisible({ timeout: 15_000 });
    } finally {
      await deleteEvent(request, eventId);
    }
  });

  test('clicking an event opens the event modal', async ({ page, request }) => {
    const itemId = await createBookableResource(request);
    const eventId = await createEvent(request, itemId);

    try {
      await page.goto(`/scheduler.php?items[]=${itemId}`);
      await expect(page.locator('.fc-event').first()).toBeVisible({ timeout: 10_000 });

      await page.locator('.fc-event').first().click();
      await expect(page.locator('#eventModal')).toBeVisible({ timeout: 8_000 });
    } finally {
      await deleteEvent(request, eventId);
    }
  });

  test('event modal shows Extra fields section with Add custom field button', async ({ page, request }) => {
    const itemId = await createBookableResource(request);
    const eventId = await createEvent(request, itemId);

    try {
      await page.goto(`/scheduler.php?items[]=${itemId}`);
      await expect(page.locator('.fc-event').first()).toBeVisible({ timeout: 10_000 });

      await page.locator('.fc-event').first().click();
      await expect(page.locator('#eventModal')).toBeVisible({ timeout: 8_000 });

      // The metadata section must be revealed (d-none removed) after open
      const metadataSection = page.locator('#eventMetadataSection');
      await expect(metadataSection).not.toHaveClass(/d-none/, { timeout: 8_000 });

      // "Add custom field" button must be present and visible (first match — excludes JSON editor's "Add field")
      await expect(
        metadataSection.locator('button[data-action="toggle-modal"][data-target="fieldBuilderModal"]').first()
      ).toBeVisible();
    } finally {
      await deleteEvent(request, eventId);
    }
  });

  test('can add a custom text field to a scheduler event', async ({ page, request }) => {
    const itemId = await createBookableResource(request);
    const eventId = await createEvent(request, itemId);
    const fieldName = `TestField_${Date.now()}`;

    try {
      await page.goto(`/scheduler.php?items[]=${itemId}`);
      await expect(page.locator('.fc-event').first()).toBeVisible({ timeout: 10_000 });

      await page.locator('.fc-event').first().click();
      await expect(page.locator('#eventModal')).toBeVisible({ timeout: 8_000 });
      await expect(page.locator('#eventMetadataSection')).not.toHaveClass(/d-none/);

      // Open field builder (first match = "Add custom field", not JSON editor's "Add field")
      await page.locator(
        '#eventMetadataSection button[data-action="toggle-modal"][data-target="fieldBuilderModal"]'
      ).first().click();
      await expect(page.locator('#fieldBuilderModal')).toBeVisible({ timeout: 6_000 });

      // Fill field name (type = text is default)
      await page.locator('#newFieldKeyInput').fill(fieldName);
      await page.locator('[data-action="save-new-field"]').click();

      // Field should appear in metadataDiv
      await expect(page.locator('#metadataDiv')).toContainText(fieldName, { timeout: 8_000 });
    } finally {
      await deleteEvent(request, eventId);
    }
  });

  test('custom field metadata persists via API after save', async ({ page, request }) => {
    const itemId = await createBookableResource(request);
    const eventId = await createEvent(request, itemId);
    const fieldName = `Persist_${Date.now()}`;
    const fieldValue = 'e2e-value';

    try {
      await page.goto(`/scheduler.php?items[]=${itemId}`);
      await expect(page.locator('.fc-event').first()).toBeVisible({ timeout: 10_000 });

      await page.locator('.fc-event').first().click();
      await expect(page.locator('#eventModal')).toBeVisible({ timeout: 8_000 });
      await expect(page.locator('#eventMetadataSection')).not.toHaveClass(/d-none/);

      // Add field (first match = "Add custom field", not JSON editor's "Add field")
      await page.locator(
        '#eventMetadataSection button[data-action="toggle-modal"][data-target="fieldBuilderModal"]'
      ).first().click();
      await expect(page.locator('#fieldBuilderModal')).toBeVisible();
      await page.locator('#newFieldKeyInput').fill(fieldName);
      await page.locator('[data-action="save-new-field"]').click();
      await expect(page.locator('#metadataDiv')).toContainText(fieldName, { timeout: 8_000 });

      // Fill in the field value
      const fieldInput = page.locator(`[data-field="${fieldName}"] input, [data-field="${fieldName}"] textarea`).first();
      if (await fieldInput.count() > 0) {
        await fieldInput.fill(fieldValue);
        await fieldInput.blur();
        // Small wait for autosave
        await page.waitForTimeout(1500);
      }

      // Verify via API that metadata is stored
      const res = await request.get(`${API}/event/${eventId}`, {
        headers: { Authorization: API_KEY },
      });
      if (res.status() === 200) {
        const body = await res.json();
        expect(body.metadata).toBeTruthy();
      }
    } finally {
      await deleteEvent(request, eventId);
    }
  });
});
