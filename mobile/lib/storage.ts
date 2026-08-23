// expo-secure-store has no real web implementation (its web build is an
// empty module), so every method throws in a browser. Native platforms keep
// using SecureStore untouched; web falls back to localStorage.
import { Platform } from "react-native";
import * as SecureStore from "expo-secure-store";

async function getItemAsync(key: string): Promise<string | null> {
  if (Platform.OS === "web") return localStorage.getItem(key);
  return SecureStore.getItemAsync(key);
}

async function setItemAsync(key: string, value: string): Promise<void> {
  if (Platform.OS === "web") {
    localStorage.setItem(key, value);
    return;
  }
  await SecureStore.setItemAsync(key, value);
}

async function deleteItemAsync(key: string): Promise<void> {
  if (Platform.OS === "web") {
    localStorage.removeItem(key);
    return;
  }
  await SecureStore.deleteItemAsync(key);
}

export default { getItemAsync, setItemAsync, deleteItemAsync };
