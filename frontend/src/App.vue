<script setup lang="ts">
import { useRoute } from 'vue-router'
import { computed } from 'vue'
import { RouterView } from 'vue-router'
import AppLayout from './layouts/AppLayout.vue'

const route = useRoute()
const isPublic = computed(() => route.meta.public === true)
</script>

<template>
  <div v-if="isPublic" class="public-page">
    <RouterView />
  </div>
  <AppLayout v-else>
    <RouterView />
  </AppLayout>
</template>

<style>
.public-page {
  width: 100%;
  height: 100vh;
}
</style>
