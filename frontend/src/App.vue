<script setup>
import { NConfigProvider, NMessageProvider } from 'naive-ui'
import AppBody from './AppBody.vue'
</script>

<template>
  <NConfigProvider>
    <NMessageProvider>
      <AppBody />
    </NMessageProvider>
  </NConfigProvider>
</template>
