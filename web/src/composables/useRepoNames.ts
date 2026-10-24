import { onMounted, ref } from "vue";
import { repoNames } from "@/api/workflows";

/** The repositories airlie knows (`owner/name`), for the type-ahead. A failed lookup is just no suggestions. */
export function useRepoNames() {
  const names = ref<string[]>([]);
  onMounted(async () => {
    try {
      names.value = await repoNames();
    } catch {
      names.value = [];
    }
  });
  return names;
}
