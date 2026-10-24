import { reactive } from "vue";

// One confirm dialog for the whole app (mounted once in the shell); callers just await an answer.
export const confirmState = reactive<{ open: boolean; message: string; resolve: ((ok: boolean) => void) | null }>({
  open: false,
  message: "",
  resolve: null,
});

export function answerConfirm(ok: boolean): void {
  const resolve = confirmState.resolve;
  confirmState.open = false;
  confirmState.resolve = null;
  resolve?.(ok);
}

export function useConfirm() {
  return {
    confirm(message: string): Promise<boolean> {
      confirmState.resolve?.(false); // a newer question replaces an unanswered one
      return new Promise<boolean>((resolve) => {
        confirmState.message = message;
        confirmState.resolve = resolve;
        confirmState.open = true;
      });
    },
  };
}
