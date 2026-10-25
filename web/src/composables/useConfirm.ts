import { reactive } from "vue";

export const DEFAULT_TITLE = "Are you sure?";
export const DEFAULT_CONFIRM = "Confirm";

// One confirm dialog for the whole app (mounted once in the shell); callers just await an answer.
export const confirmState = reactive<{
  open: boolean;
  message: string;
  title: string;
  confirmLabel: string;
  resolve: ((ok: boolean) => void) | null;
}>({
  open: false,
  message: "",
  title: DEFAULT_TITLE,
  confirmLabel: DEFAULT_CONFIRM,
  resolve: null,
});

export function answerConfirm(ok: boolean): void {
  const resolve = confirmState.resolve;
  confirmState.open = false;
  confirmState.resolve = null;
  resolve?.(ok);
}

export type ConfirmOptions = { title?: string; confirmLabel?: string };

export function useConfirm() {
  return {
    confirm(message: string, options: ConfirmOptions = {}): Promise<boolean> {
      confirmState.resolve?.(false); // a newer question replaces an unanswered one
      return new Promise<boolean>((resolve) => {
        confirmState.message = message;
        confirmState.title = options.title ?? DEFAULT_TITLE;
        confirmState.confirmLabel = options.confirmLabel ?? DEFAULT_CONFIRM;
        confirmState.resolve = resolve;
        confirmState.open = true;
      });
    },
  };
}
