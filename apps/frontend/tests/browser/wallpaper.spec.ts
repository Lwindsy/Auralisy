// The desktop wallpaper page (wallpaper.html) against the synthetic fixture backend: no
// Java, no model calls. Wallpaper Engine passes only the mouse, so the page asks only for
// genres, shows one record a day and never asks for new picks on its own.
import {
  expect,
  test,
  type BrowserContext,
  type Route,
} from "@playwright/test";
import { emptyInput } from "../../src/state/input.ts";
import { FixtureBackend, JAZZ_GENRE_ID, ready } from "../fixtures.ts";

async function attach(
  context: BrowserContext,
  backend: FixtureBackend,
  intercept?: (route: Route) => Promise<boolean>,
) {
  await context.route("**/v1/**", async (route) => {
    const request = route.request();
    if (intercept && (await intercept(route))) return;
    const response = backend.handle(
      request.method(),
      request.url(),
      request.postDataJSON() ?? undefined,
      request.headers(),
    );
    await route.fulfill({
      status: response.status,
      contentType: "application/json",
      body: JSON.stringify(response.body),
    });
  });
}

const posts = (backend: FixtureBackend, suffix: string) =>
  backend.calls.filter(
    (call) => call.method === "POST" && call.path.endsWith(suffix),
  );

test("asks only for genres, browsed with the mouse", async ({
  page,
  context,
}) => {
  const backend = new FixtureBackend();
  await attach(context, backend);
  await page.goto("/wallpaper.html?lang=zh-CN");

  await expect(page.locator(".door")).toHaveCount(1);
  await expect(page.locator(".door[data-door='genres']")).toHaveAttribute(
    "data-state",
    "open",
  );
  await expect(page.locator("textarea")).toHaveCount(0);
  await expect(page.locator("#genres-query")).toHaveCount(0);

  await page.getByRole("button", { name: "喜欢爵士", exact: true }).click();
  await page.getByRole("button", { name: "开始发现" }).click();
  await expect(page.locator(".wall")).toBeVisible({ timeout: 15000 });

  const submitted = posts(backend, "/v1/discovery-sessions")[0]?.body as {
    input: {
      liked_genres: { genre_id: string }[];
      liked_albums: unknown[];
      request_text: string;
      language: string;
    };
  };
  expect(submitted.input.liked_genres.map((item) => item.genre_id)).toEqual([
    JAZZ_GENRE_ID,
  ]);
  expect(submitted.input.liked_albums).toEqual([]);
  expect(submitted.input.request_text).toBe("");
  expect(submitted.input.language).toBe("zh-CN");
});

test("keeps the day's record across a reload; steps, heard and undo are clicks", async ({
  page,
  context,
}) => {
  const backend = new FixtureBackend(ready());
  await attach(context, backend);
  await page.goto("/wallpaper.html?lang=zh-CN&fps=12");

  const title = page.locator(".wall__title");
  await expect(title).toHaveText("样例专辑 1");
  await expect(page.locator(".wall__foot")).toContainText("01 / 03");

  await page.locator(".wall").hover();
  await page.getByRole("button", { name: "下一张", exact: true }).click();
  await expect(title).toHaveText("样例专辑 2");
  const stored = await page.evaluate(() =>
    localStorage.getItem("auralisy.wallpaper.pick.v5"),
  );
  expect(JSON.parse(stored ?? "{}")).toMatchObject({
    candidate: "candidate-2",
    // The server's place (slot-0002) with the session's fingerprint, not the page order.
    place: expect.stringMatching(/^[0-9a-f]{8}:2$/),
  });
  // Only an opaque id, a date and a place: never the input or the credential.
  expect(stored).not.toContain("session");

  await page.reload();
  await expect(title).toHaveText("样例专辑 2");

  await page.locator(".wall").hover();
  await page.getByRole("button", { name: "听过了", exact: true }).click();
  await expect(page.locator(".wall")).toHaveAttribute("data-heard", "true");
  expect(posts(backend, "/actions")).toHaveLength(1);
  await page.getByRole("button", { name: "撤销", exact: true }).click();
  await expect(page.locator(".wall")).not.toHaveAttribute("data-heard", "true");

  // Stepping past the last record ends the set instead of asking for more.
  await page.getByRole("button", { name: "下一张", exact: true }).click();
  await page.getByRole("button", { name: "下一张", exact: true }).click();
  await expect(page.locator(".wall__finish")).toContainText("这一组看完了");
  expect(posts(backend, "/next-batch")).toHaveLength(0);
});

test("opens in English when the launcher asks for it", async ({
  page,
  context,
}) => {
  await attach(context, new FixtureBackend(ready()));
  await page.goto("/wallpaper.html?lang=en");
  await expect(page.locator(".wall__eyebrow")).toContainText("Today's record");
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
});

test("a lost answer keeps its key and is retried from the toast", async ({
  page,
  context,
}) => {
  const backend = new FixtureBackend(ready());
  let lost = true;
  await attach(context, backend, async (route) => {
    const request = route.request();
    if (lost && request.url().endsWith("/actions")) {
      lost = false;
      backend.handle(
        request.method(),
        request.url(),
        request.postDataJSON(),
        request.headers(),
      );
      await route.abort("failed");
      return true;
    }
    return false;
  });
  await page.goto("/wallpaper.html?lang=zh-CN");
  await page.locator(".wall").hover();
  await page.getByRole("button", { name: "听过了", exact: true }).click();
  const toast = page.locator(".wall-toast");
  await expect(toast).toBeVisible();
  await toast.getByRole("button", { name: "重试原操作", exact: true }).click();
  await expect(toast).toHaveCount(0);
  const calls = posts(backend, "/actions");
  expect(calls).toHaveLength(2);
  expect(calls[0]!.headers["idempotency-key"]).toBe(
    calls[1]!.headers["idempotency-key"],
  );
});

test("a refused 再来一组 never opens a new session on its own", async ({
  page,
  context,
}) => {
  const backend = new FixtureBackend({
    ...ready(),
    editable_input: {
      ...emptyInput(),
      liked_genres: [
        { genre_id: JAZZ_GENRE_ID, strength: "normal", focused: false },
      ],
    },
  });
  await attach(context, backend, async (route) => {
    if (!route.request().url().endsWith("/next-batch")) return false;
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({
        code: "BATCH_UNAVAILABLE",
        request_id: "fixture",
        message_key: "batch.unavailable",
        retryable: false,
        idempotency_consumed: false,
        current_revision: null,
        retry_disposition: "NEW_EXPLICIT_INTENT",
      }),
    });
    return true;
  });
  await page.goto("/wallpaper.html?lang=zh-CN");
  await page.locator(".wall").hover();
  for (let step = 0; step < 3; step++)
    await page.getByRole("button", { name: "下一张", exact: true }).click();
  await page.getByRole("button", { name: "再来一组", exact: true }).click();
  const restart = page.getByRole("button", {
    name: "用同样的风格重新开始",
    exact: true,
  });
  await expect(restart).toBeVisible();
  expect(posts(backend, "/v1/discovery-sessions")).toHaveLength(0);
  await restart.click();
  await expect
    .poll(() => posts(backend, "/v1/discovery-sessions").length)
    .toBe(1);
});

test("a narrow portrait screen can still pick a genre", async ({
  page,
  context,
}) => {
  const backend = new FixtureBackend();
  await attach(context, backend);
  await page.setViewportSize({ width: 720, height: 1280 });
  await page.goto("/wallpaper.html?lang=zh-CN");
  const list = page.locator(".genres__list");
  await expect(list).toBeVisible();
  const box = await list.boundingBox();
  expect(box?.height ?? 0).toBeGreaterThan(200);
  await page.getByRole("button", { name: "喜欢爵士", exact: true }).click();
  await page.getByRole("button", { name: "开始发现" }).click();
  await expect(page.locator(".wall")).toBeVisible({ timeout: 15000 });
});

test("a new session after a reload starts at its first record", async ({
  page,
  context,
}) => {
  await attach(context, new FixtureBackend(ready()));
  await context.addInitScript(() => {
    if (!sessionStorage.getItem("seeded")) {
      sessionStorage.setItem("seeded", "1");
      localStorage.setItem(
        "auralisy.wallpaper.pick",
        JSON.stringify({
          candidate: "old-session-3",
          day: "2026-01-01",
          index: 2,
        }),
      );
    }
  });
  await page.goto("/wallpaper.html?lang=zh-CN");
  await expect(page.locator(".wall__title")).toHaveText("样例专辑 1");
});

test("after 听过了 the replacement takes its place, also after a reload", async ({
  page,
  context,
}) => {
  await attach(context, new FixtureBackend(ready()));
  await page.goto("/wallpaper.html?lang=zh-CN");
  await page.locator(".wall").hover();
  await page.getByRole("button", { name: "听过了", exact: true }).click();
  await expect(page.locator(".wall")).toHaveAttribute("data-heard", "true");
  // The server fills the same place; the fixture numbers replacements from 10.
  await expect(page.locator(".wall__title")).toHaveText("样例专辑 10");
  await expect(page.locator(".wall__foot")).toContainText("01 / 03");
  await page.reload();
  await expect(page.locator(".wall__title")).toHaveText("样例专辑 10");
});

test("an undo whose answer was lost is cleared once its retry succeeds", async ({
  page,
  context,
}) => {
  const backend = new FixtureBackend(ready());
  let lose = false;
  await attach(context, backend, async (route) => {
    const request = route.request();
    if (lose && request.url().endsWith("/undo-last-card")) {
      lose = false;
      backend.handle(
        request.method(),
        request.url(),
        request.postDataJSON(),
        request.headers(),
      );
      await route.abort("failed");
      return true;
    }
    return false;
  });
  await page.goto("/wallpaper.html?lang=zh-CN");
  await page.locator(".wall").hover();
  await page.getByRole("button", { name: "听过了", exact: true }).click();
  await expect(page.locator(".wall")).toHaveAttribute("data-heard", "true");
  lose = true;
  await page.getByRole("button", { name: "撤销", exact: true }).click();
  const toast = page.locator(".wall-toast");
  await toast.getByRole("button", { name: "重试原操作", exact: true }).click();
  await expect(toast).toHaveCount(0);
  await expect(page.locator(".wall")).not.toHaveAttribute("data-heard", "true");
  await expect(
    page.getByRole("button", { name: "听过了", exact: true }),
  ).toBeEnabled();
});

test("a refused 再来一组 keeps the day's record and reads state when told to", async ({
  page,
  context,
}) => {
  const backend = new FixtureBackend({
    ...ready(),
    continuation_tail_id: "candidate-3",
  });
  await attach(context, backend, async (route) => {
    if (!route.request().url().endsWith("/next-batch")) return false;
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({
        code: "REVISION_CONFLICT",
        request_id: "fixture",
        message_key: "revision.conflict",
        retryable: false,
        idempotency_consumed: false,
        current_revision: 1,
        retry_disposition: "POLL_STATE",
      }),
    });
    return true;
  });
  await page.goto("/wallpaper.html?lang=zh-CN");
  await page.locator(".wall").hover();
  for (let step = 0; step < 3; step++)
    await page.getByRole("button", { name: "下一张", exact: true }).click();
  await expect(page.locator(".wall__finish")).toBeVisible();
  const before = await page.evaluate(() =>
    localStorage.getItem("auralisy.wallpaper.pick.v5"),
  );
  const reads = () =>
    backend.calls.filter(
      (call) => call.method === "GET" && call.path.endsWith("/current-state"),
    ).length;
  const readsBefore = reads();
  await page.getByRole("button", { name: "再来一组", exact: true }).click();
  await expect.poll(reads).toBeGreaterThan(readsBefore);
  expect(posts(backend, "/v1/discovery-sessions")).toHaveLength(0);
  expect(
    await page.evaluate(() =>
      localStorage.getItem("auralisy.wallpaper.pick.v5"),
    ),
  ).toBe(before);
  await page.reload();
  await expect(page.locator(".wall__title")).toHaveText("样例专辑 3");
  await expect(page.locator(".wall__finish")).toBeVisible();
});

test("a refused new session keeps the day's record on disk", async ({
  page,
  context,
}) => {
  const backend = new FixtureBackend({
    ...ready(),
    editable_input: {
      ...emptyInput(),
      liked_genres: [
        { genre_id: JAZZ_GENRE_ID, strength: "normal", focused: false },
      ],
    },
  });
  await attach(context, backend, async (route) => {
    const request = route.request();
    if (
      request.method() !== "POST" ||
      !request.url().endsWith("/v1/discovery-sessions")
    )
      return false;
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({
        code: "SESSION_LIMIT",
        request_id: "fixture",
        message_key: "session.limit",
        retryable: false,
        idempotency_consumed: false,
        current_revision: null,
        retry_disposition: "STOP",
      }),
    });
    return true;
  });
  await page.goto("/wallpaper.html?lang=zh-CN");
  await page.locator(".wall").hover();
  for (let step = 0; step < 3; step++)
    await page.getByRole("button", { name: "下一张", exact: true }).click();
  await page.getByRole("button", { name: "换风格", exact: true }).click();
  await page.getByRole("button", { name: "开始发现" }).click();
  await expect(page.locator(".wall-toast")).toBeVisible();
  await page.reload();
  await expect(page.locator(".wall__title")).toHaveText("样例专辑 3");
  await expect(page.locator(".wall__finish")).toBeVisible();
});

test("a record the set gains after a refused 再来一组 is not taken for the answer", async ({
  page,
  context,
}) => {
  const backend = new FixtureBackend(ready());
  await attach(context, backend, async (route) => {
    if (!route.request().url().endsWith("/next-batch")) return false;
    // The server refuses, but the set it keeps has gained a fourth record meanwhile.
    backend.state = {
      ...ready(4, 4),
      revision: backend.state.revision + 1,
    };
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({
        code: "REVISION_CONFLICT",
        request_id: "fixture",
        message_key: "revision.conflict",
        retryable: false,
        idempotency_consumed: false,
        current_revision: backend.state.revision,
        retry_disposition: "POLL_STATE",
      }),
    });
    return true;
  });
  await page.goto("/wallpaper.html?lang=zh-CN");
  await page.locator(".wall").hover();
  for (let step = 0; step < 3; step++)
    await page.getByRole("button", { name: "下一张", exact: true }).click();
  await page.getByRole("button", { name: "再来一组", exact: true }).click();
  await expect(page.locator(".wall__foot")).toContainText("03 / 04");
  await expect(page.locator(".wall__title")).toHaveText("样例专辑 3");
  await expect(page.locator(".wall__finish")).toHaveCount(0);
  await page.reload();
  await expect(page.locator(".wall__title")).toHaveText("样例专辑 3");
});

test("a 听过了 that never reached the server keeps today's record on disk", async ({
  page,
  context,
}) => {
  const backend = new FixtureBackend(ready());
  await attach(context, backend, async (route) => {
    if (!route.request().url().endsWith("/actions")) return false;
    await route.abort("failed");
    return true;
  });
  await page.goto("/wallpaper.html?lang=zh-CN");
  await page.locator(".wall").hover();
  await page.getByRole("button", { name: "下一张", exact: true }).click();
  await page.getByRole("button", { name: "听过了", exact: true }).click();
  await expect(page.locator(".wall-toast")).toBeVisible();
  expect(
    backend.calls.filter((call) => call.path.endsWith("/actions")),
  ).toHaveLength(0);
  await page.reload();
  await expect(page.locator(".wall__title")).toHaveText("样例专辑 2");
});

test("听过了 on the last record: the replacement goes up in its place", async ({
  page,
  context,
}) => {
  await attach(context, new FixtureBackend(ready()));
  await page.goto("/wallpaper.html?lang=zh-CN");
  await page.locator(".wall").hover();
  for (let step = 0; step < 2; step++)
    await page.getByRole("button", { name: "下一张", exact: true }).click();
  await expect(page.locator(".wall__title")).toHaveText("样例专辑 3");
  await page.getByRole("button", { name: "听过了", exact: true }).click();
  await expect(page.locator(".wall")).toHaveAttribute("data-heard", "true");
  await expect(page.locator(".wall__title")).toHaveText("样例专辑 10");
  await expect(page.locator(".wall__foot")).toContainText("03 / 03");
  await page.reload();
  await expect(page.locator(".wall__title")).toHaveText("样例专辑 10");
});

test("a reload while the place is still being filled keeps the marker until it is", async ({
  page,
  context,
}) => {
  const backend = new FixtureBackend(ready());
  await attach(context, backend);
  await page.goto("/wallpaper.html?lang=zh-CN");
  await page.locator(".wall").hover();
  for (let step = 0; step < 2; step++)
    await page.getByRole("button", { name: "下一张", exact: true }).click();
  const marker = () =>
    page.evaluate(() => localStorage.getItem("auralisy.wallpaper.pick.v5"));
  const before = await marker();
  // Mark the third record heard on the server while the page is away, and hold the
  // replacement back: the place is still being filled when the page comes back.
  await page.goto("about:blank");
  const slot = backend.state.slots[2]!;
  slot.card = null;
  slot.status = "SUPPLYING";
  backend.state.supply_state = "SUPPLYING";
  backend.state.revision++;
  await page.goto("/wallpaper.html?lang=zh-CN");
  await expect(page.locator(".wall__title")).toHaveText("样例专辑 2");
  expect(await marker()).toBe(before);
  // The replacement arrives: it goes up in the place the marker names.
  slot.card = {
    ...backend.state.slots[0]!.card!,
    candidate_id: "late",
    identity: {
      ...backend.state.slots[0]!.card!.identity,
      title: "迟到的替补",
    },
  };
  slot.status = "READY";
  backend.state.supply_state = "SUFFICIENT";
  backend.state.revision++;
  await page.reload();
  await expect(page.locator(".wall__title")).toHaveText("迟到的替补");
});

test("a marker from another session never picks a place in the new one", async ({
  page,
  context,
}) => {
  const backend = new FixtureBackend(ready());
  await attach(context, backend);
  await page.goto("/wallpaper.html?lang=zh-CN");
  await page.locator(".wall").hover();
  for (let step = 0; step < 3; step++)
    await page.getByRole("button", { name: "下一张", exact: true }).click();
  await expect(page.locator(".wall__finish")).toBeVisible();
  // While the page is away the server holds another session, numbering its places
  // from slot-0001 again.
  await page.goto("about:blank");
  backend.state = {
    ...backend.state,
    session_id: "session-2",
    revision: backend.state.revision + 1,
    slots: backend.state.slots.map((slot, i) => ({
      ...slot,
      card: {
        ...slot.card!,
        candidate_id: `new-${i + 10}`,
        identity: { ...slot.card!.identity, title: `新会话 ${i + 10}` },
      },
    })),
  };
  await page.goto("/wallpaper.html?lang=zh-CN");
  await expect(page.locator(".wall__title")).toHaveText("新会话 10");
});

test("a replacement can be marked heard again without a reload", async ({
  page,
  context,
}) => {
  const backend = new FixtureBackend(ready());
  await attach(context, backend);
  await page.goto("/wallpaper.html?lang=zh-CN");
  await page.locator(".wall").hover();
  const heard = page.getByRole("button", { name: "听过了", exact: true });
  await heard.click();
  await expect(page.locator(".wall__title")).toHaveText("样例专辑 10");
  await expect(heard).toBeEnabled();
  await heard.click();
  await expect(page.locator(".wall__title")).toHaveText("样例专辑 11");
  expect(posts(backend, "/actions")).toHaveLength(2);
  await expect(page.locator(".wall__foot")).toContainText("01 / 03");
});

test("undo stays reachable when the place could not be filled", async ({
  page,
  context,
}) => {
  const backend = new FixtureBackend(ready());
  await attach(context, backend, async (route) => {
    const request = route.request();
    if (!request.url().endsWith("/actions")) return false;
    backend.handle(
      request.method(),
      request.url(),
      request.postDataJSON(),
      request.headers(),
    );
    // Nothing left to supply: the place stays empty.
    const slot = backend.state.slots.find((entry) => !entry.card)!;
    slot.status = "EMPTY";
    (backend as unknown as { pendingSupply: boolean }).pendingSupply = false;
    backend.state.supply_state = "FINAL_EXHAUSTED";
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(backend.state),
    });
    return true;
  });
  await page.goto("/wallpaper.html?lang=zh-CN");
  await page.locator(".wall").hover();
  for (let step = 0; step < 2; step++)
    await page.getByRole("button", { name: "下一张", exact: true }).click();
  await page.getByRole("button", { name: "听过了", exact: true }).click();
  const undo = page.getByRole("button", { name: "撤销", exact: true });
  await expect(undo).toBeVisible();
  await undo.click();
  await expect(page.locator(".wall__title")).toHaveText("样例专辑 3");
  await expect(page.locator(".wall")).not.toHaveAttribute("data-heard", "true");
});

test("the position counts places that are being filled", async ({
  page,
  context,
}) => {
  const backend = new FixtureBackend(ready());
  await attach(context, backend, async (route) => {
    const request = route.request();
    if (request.method() !== "GET" || !request.url().endsWith("/current-state"))
      return false;
    // Hold the replacement back: the place stays supplying.
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(backend.state),
    });
    return true;
  });
  await page.goto("/wallpaper.html?lang=zh-CN");
  await page.locator(".wall").hover();
  for (let step = 0; step < 2; step++)
    await page.getByRole("button", { name: "下一张", exact: true }).click();
  await page.getByRole("button", { name: "听过了", exact: true }).click();
  await expect(page.locator(".wall")).toHaveAttribute("data-heard", "true");
  await expect(page.locator(".wall__title")).toHaveText("样例专辑 3");
  await expect(page.locator(".wall__foot")).toContainText("03 / 03");
});

/** The wallpaper's day as the page computes it: it turns at 08:00. */
const wallpaperToday = () => {
  const shifted = new Date();
  shifted.setHours(shifted.getHours() - 8);
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${shifted.getFullYear()}-${pad(shifted.getMonth() + 1)}-${pad(shifted.getDate())}`;
};

test("a new session sharing an album with the old one still starts at its first record", async ({
  page,
  context,
}) => {
  const backend = new FixtureBackend(ready());
  await attach(context, backend);
  await page.goto("/wallpaper.html?lang=zh-CN");
  await page.locator(".wall").hover();
  for (let step = 0; step < 3; step++)
    await page.getByRole("button", { name: "下一张", exact: true }).click();
  await expect(page.locator(".wall__finish")).toBeVisible();
  await page.goto("about:blank");
  const [first, second, third] = backend.state.slots;
  backend.state = {
    ...backend.state,
    session_id: "session-2",
    revision: backend.state.revision + 1,
    slots: [
      {
        ...first!,
        card: {
          ...first!.card!,
          candidate_id: "new-10",
          identity: { ...first!.card!.identity, title: "新会话 10" },
        },
      },
      // The album the old session ended on turns up again, in place 2.
      { ...second!, card: third!.card },
      {
        ...third!,
        card: {
          ...third!.card!,
          candidate_id: "new-12",
          identity: { ...third!.card!.identity, title: "新会话 12" },
        },
      },
    ],
  };
  await page.goto("/wallpaper.html?lang=zh-CN");
  await expect(page.locator(".wall__title")).toHaveText("新会话 10");
});

test("an earlier 'after' marker survives reloads while its place is filled", async ({
  page,
  context,
}) => {
  const backend = new FixtureBackend(ready());
  const slot = backend.state.slots[2]!;
  slot.card = null;
  slot.status = "SUPPLYING";
  backend.state.supply_state = "SUPPLYING";
  await attach(context, backend, async (route) => {
    const request = route.request();
    if (request.method() !== "GET" || !request.url().endsWith("/current-state"))
      return false;
    // Hold the replacement back until the test releases it.
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(backend.state),
    });
    return true;
  });
  const today = wallpaperToday();
  await context.addInitScript((day) => {
    if (!sessionStorage.getItem("seeded")) {
      sessionStorage.setItem("seeded", "1");
      localStorage.setItem(
        "auralisy.wallpaper.pick.v3",
        JSON.stringify({ candidate: "candidate-2", day, index: -3 }),
      );
    }
  }, today);
  const stored = () =>
    page.evaluate(() => ({
      v3: localStorage.getItem("auralisy.wallpaper.pick.v3"),
      v5: localStorage.getItem("auralisy.wallpaper.pick.v5"),
    }));
  await page.goto("/wallpaper.html?lang=zh-CN");
  await expect(page.locator(".wall__title")).toHaveText("样例专辑 2");
  await page.reload();
  await expect(page.locator(".wall__title")).toHaveText("样例专辑 2");
  await page.reload();
  await expect(page.locator(".wall__title")).toHaveText("样例专辑 2");
  expect(await stored()).toEqual({
    v3: JSON.stringify({ candidate: "candidate-2", day: today, index: -3 }),
    v5: null,
  });
  slot.card = {
    ...backend.state.slots[0]!.card!,
    candidate_id: "late",
    identity: {
      ...backend.state.slots[0]!.card!.identity,
      title: "迟到的替补",
    },
  };
  slot.status = "READY";
  backend.state.supply_state = "SUFFICIENT";
  backend.state.revision++;
  await page.reload();
  await expect(page.locator(".wall__title")).toHaveText("迟到的替补");
  const after = await stored();
  expect(after.v3).toBeNull();
  expect(JSON.parse(after.v5 ?? "{}")).toMatchObject({ candidate: "late" });
});

test("a set with nothing left shows the end, with undo and change genres", async ({
  page,
  context,
}) => {
  const backend = new FixtureBackend(ready(1, 1));
  await attach(context, backend, async (route) => {
    const request = route.request();
    if (!request.url().endsWith("/actions")) return false;
    backend.handle(
      request.method(),
      request.url(),
      request.postDataJSON(),
      request.headers(),
    );
    const slot = backend.state.slots.find((entry) => !entry.card)!;
    slot.status = "EMPTY";
    (backend as unknown as { pendingSupply: boolean }).pendingSupply = false;
    backend.state.supply_state = "FINAL_EXHAUSTED";
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(backend.state),
    });
    return true;
  });
  await page.goto("/wallpaper.html?lang=zh-CN");
  await page.locator(".wall").hover();
  await page.getByRole("button", { name: "听过了", exact: true }).click();
  await expect(page.locator(".wall--empty")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "换风格", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "撤销", exact: true }).click();
  await expect(page.locator(".wall__title")).toHaveText("样例专辑 1");
});

test("‹ steps from the dimmed record waiting for its replacement", async ({
  page,
  context,
}) => {
  const backend = new FixtureBackend(ready());
  await attach(context, backend, async (route) => {
    const request = route.request();
    if (request.method() !== "GET" || !request.url().endsWith("/current-state"))
      return false;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(backend.state),
    });
    return true;
  });
  await page.goto("/wallpaper.html?lang=zh-CN");
  await page.locator(".wall").hover();
  for (let step = 0; step < 2; step++)
    await page.getByRole("button", { name: "下一张", exact: true }).click();
  await page.getByRole("button", { name: "听过了", exact: true }).click();
  await expect(page.locator(".wall")).toHaveAttribute("data-heard", "true");
  await expect(page.locator(".wall__title")).toHaveText("样例专辑 3");
  await page.getByRole("button", { name: "上一张", exact: true }).click();
  await expect(page.locator(".wall__title")).toHaveText("样例专辑 2");
});

test("a place whose filling failed keeps its spot, and an undo brings its record back", async ({
  page,
  context,
}) => {
  const backend = new FixtureBackend(ready());
  await attach(context, backend, async (route) => {
    const request = route.request();
    if (!request.url().endsWith("/actions")) return false;
    backend.handle(
      request.method(),
      request.url(),
      request.postDataJSON(),
      request.headers(),
    );
    const slot = backend.state.slots.find((entry) => !entry.card)!;
    slot.status = "FAILED";
    (backend as unknown as { pendingSupply: boolean }).pendingSupply = false;
    backend.state.supply_state = "SUFFICIENT";
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(backend.state),
    });
    return true;
  });
  await page.goto("/wallpaper.html?lang=zh-CN");
  await page.locator(".wall").hover();
  for (let step = 0; step < 2; step++)
    await page.getByRole("button", { name: "下一张", exact: true }).click();
  await page.getByRole("button", { name: "听过了", exact: true }).click();
  await expect(page.locator(".wall__foot")).toContainText("/ 03");
  const marker = () =>
    page.evaluate(() =>
      JSON.parse(localStorage.getItem("auralisy.wallpaper.pick.v5") ?? "{}"),
    );
  expect((await marker()).place).toMatch(/:3$/);
  await page.getByRole("button", { name: "撤销", exact: true }).click();
  await expect(page.locator(".wall__title")).toHaveText("样例专辑 3");
  await expect(page.locator(".wall__foot")).toContainText("03 / 03");
  expect((await marker()).place).toMatch(/:3$/);
});
