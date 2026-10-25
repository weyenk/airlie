<script setup lang="ts">
import { answerConfirm, confirmState } from "@/composables/useConfirm";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";

// Escape and a click outside both arrive as open=false: that is a "no"
function onOpen(open: boolean) {
  if (!open) answerConfirm(false);
}
</script>

<template>
  <Dialog :open="confirmState.open" @update:open="onOpen">
    <DialogContent :show-close-button="false">
      <DialogHeader>
        <DialogTitle>Are you sure?</DialogTitle>
        <DialogDescription>{{ confirmState.message }}</DialogDescription>
      </DialogHeader>
      <DialogFooter>
        <Button variant="outline" data-confirm-cancel @click="answerConfirm(false)">Cancel</Button>
        <Button data-confirm-ok @click="answerConfirm(true)">Confirm</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
