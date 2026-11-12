<script setup lang="ts">
import { useRoute } from "vue-router";
import AppShell from "@/components/AppShell.vue";
import UpdateToast from "@/components/UpdateToast.vue";

const route = useRoute();
</script>

<template>
  <RouterView v-if="route.meta.bare" />
  <AppShell v-else><RouterView /></AppShell>
  <UpdateToast />
</template>
