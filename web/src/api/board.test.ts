import { addComment, createItem, editComment, getItem, listItems, moveItem, patchItem } from "./board";
import { callOf, stubJson } from "./fetch-stub";

afterEach(() => vi.unstubAllGlobals());

test("listItems unwraps the items and passes the filters it is given", async () => {
  const fetchMock = stubJson({ items: [{ key: "AIR-1" }] });

  await expect(listItems({ level: "story", parent: "AIR-2" })).resolves.toEqual([{ key: "AIR-1" }]);
  expect(callOf(fetchMock).url).toBe("/v1/board/items?level=story&parent=AIR-2");

  await listItems();
  expect(callOf(fetchMock, 1).url).toBe("/v1/board/items");
});

test("getItem, createItem and patchItem use the item routes", async () => {
  const fetchMock = stubJson({ key: "AIR-1" });

  await getItem("AIR-1");
  await createItem({ level: "story", title: "T", description: "D", parent: "AIR-2", labels: ["repo:a/b"] });
  await patchItem("AIR-1", { title: "New", status: "doing" });

  expect(callOf(fetchMock, 0)).toMatchObject({ url: "/v1/board/items/AIR-1", method: "GET" });
  expect(callOf(fetchMock, 1)).toMatchObject({
    url: "/v1/board/items",
    method: "POST",
    body: { level: "story", title: "T", description: "D", parent: "AIR-2", labels: ["repo:a/b"] },
  });
  expect(callOf(fetchMock, 2)).toMatchObject({
    url: "/v1/board/items/AIR-1",
    method: "PATCH",
    body: { title: "New", status: "doing" },
  });
});

test("moveItem, addComment and editComment use the move and comment routes", async () => {
  const fetchMock = stubJson({ key: "AIR-1" });

  await moveItem("AIR-1", "review", "AIR-3");
  await moveItem("AIR-1", "done");
  await addComment("AIR-1", "hello");
  await editComment("AIR-1", 7, "edited");

  expect(callOf(fetchMock, 0)).toMatchObject({
    url: "/v1/board/items/AIR-1/move",
    method: "POST",
    body: { status: "review", before: "AIR-3" },
  });
  expect(callOf(fetchMock, 1).body).toEqual({ status: "done", before: null });
  expect(callOf(fetchMock, 2)).toMatchObject({
    url: "/v1/board/items/AIR-1/comments",
    method: "POST",
    body: { text: "hello" },
  });
  expect(callOf(fetchMock, 3)).toMatchObject({
    url: "/v1/board/items/AIR-1/comments/7",
    method: "PATCH",
    body: { text: "edited" },
  });
});

test("a key is escaped into the path", async () => {
  const fetchMock = stubJson({});
  await getItem("a/b");
  expect(callOf(fetchMock).url).toBe("/v1/board/items/a%2Fb");
});
