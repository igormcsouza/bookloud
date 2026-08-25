import { Modal, Pressable, Text, View } from "react-native";

// A lightweight in-app confirm, not the native `Alert` (issue #12's ask):
// `Alert` renders the platform's system chrome, which clashes with the
// design language everywhere else in this app.
type Props = {
  visible: boolean;
  title: string;
  message: string;
  confirmLabel: string;
  onConfirm: () => void;
  onCancel: () => void;
};

export function ConfirmDialog({ visible, title, message, confirmLabel, onConfirm, onCancel }: Props) {
  return (
    <Modal visible={visible} transparent animationType="fade" onRequestClose={onCancel}>
      <Pressable
        onPress={onCancel}
        className="flex-1 bg-black/40 items-center justify-center px-8"
      >
        <Pressable
          onPress={(e) => e.stopPropagation()}
          className="w-full bg-surface dark:bg-dsurface rounded-md p-5 gap-4"
        >
          <View className="gap-1.5">
            <Text
              className="text-[16px] text-ink dark:text-dink"
              style={{ fontFamily: "Fraunces_600SemiBold" }}
            >
              {title}
            </Text>
            <Text className="text-[13.5px] text-ink-muted dark:text-dink-muted">{message}</Text>
          </View>

          <View className="flex-row justify-end gap-4">
            <Pressable onPress={onCancel} hitSlop={8}>
              <Text className="text-[13px] text-ink-muted dark:text-dink-muted" style={{ fontFamily: "Karla_700Bold" }}>
                Cancel
              </Text>
            </Pressable>
            <Pressable onPress={onConfirm} hitSlop={8}>
              <Text className="text-[13px] text-brick dark:text-dbrick" style={{ fontFamily: "Karla_700Bold" }}>
                {confirmLabel}
              </Text>
            </Pressable>
          </View>
        </Pressable>
      </Pressable>
    </Modal>
  );
}
