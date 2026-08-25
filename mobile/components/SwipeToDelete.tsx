import { useRef } from "react";
import { Animated, Pressable, Text, View } from "react-native";
import { Swipeable } from "react-native-gesture-handler";
import { Trash2 } from "lucide-react-native";

// The reveal action's fixed width -- deliberately not "oversized" (issue
// #12's ask): a stubby brick square, roughly the height of one BookCard row,
// not a full-width red bar.
const ACTION_WIDTH = 72;

type Props = {
  onDelete: () => void;
  children: React.ReactNode;
  /** Disables the swipe while a delete for this row (or an upload/retry
   *  elsewhere) is already in flight. */
  disabled?: boolean;
};

export function SwipeToDelete({ onDelete, children, disabled }: Props) {
  const ref = useRef<Swipeable>(null);

  function renderRightActions(progress: Animated.AnimatedInterpolation<number>) {
    const scale = progress.interpolate({
      inputRange: [0, 1],
      outputRange: [0.6, 1],
      extrapolate: "clamp",
    });
    return (
      <Pressable
        onPress={() => {
          ref.current?.close();
          onDelete();
        }}
        className="bg-brick dark:bg-dbrick items-center justify-center rounded-md ml-2"
        style={{ width: ACTION_WIDTH }}
      >
        <Animated.View style={{ transform: [{ scale }], alignItems: "center", gap: 2 }}>
          <Trash2 size={18} color="#FBF8F1" strokeWidth={2.25} />
          <Text className="text-[10px] text-[#FBF8F1]" style={{ fontFamily: "Karla_700Bold" }}>
            Delete
          </Text>
        </Animated.View>
      </Pressable>
    );
  }

  return (
    <Swipeable
      ref={ref}
      renderRightActions={disabled ? undefined : renderRightActions}
      overshootRight={false}
      rightThreshold={40}
    >
      <View>{children}</View>
    </Swipeable>
  );
}
