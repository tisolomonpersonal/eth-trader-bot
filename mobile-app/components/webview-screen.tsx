import React, { useRef, useState } from "react";
import { View, ActivityIndicator, Text, TouchableOpacity } from "react-native";
import { WebView } from "react-native-webview";
import { useColors } from "@/hooks/use-colors";
import { cn } from "@/lib/utils";

interface WebViewScreenProps {
  url: string;
  className?: string;
}

export function WebViewScreen({ url, className }: WebViewScreenProps) {
  const colors = useColors();
  const webViewRef = useRef<WebView>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  const handleError = () => {
    setError(true);
    setLoading(false);
  };

  const handleLoadStart = () => {
    setLoading(true);
    setError(false);
  };

  const handleLoadEnd = () => {
    setLoading(false);
  };

  const handleRetry = () => {
    setError(false);
    setLoading(true);
    webViewRef.current?.reload();
  };

  if (error) {
    return (
      <View
        className={cn(
          "flex-1 items-center justify-center p-6",
          className
        )}
        style={{ backgroundColor: colors.background }}
      >
        <Text className="text-lg font-semibold text-foreground mb-2">
          Unable to Load
        </Text>
        <Text className="text-sm text-muted text-center mb-6">
          There was an error loading the chat. Please check your connection and try again.
        </Text>
        <TouchableOpacity
          onPress={handleRetry}
          className="bg-primary px-6 py-3 rounded-full"
        >
          <Text className="text-white font-semibold">Retry</Text>
        </TouchableOpacity>
      </View>
    );
  }

  return (
    <View
      className={cn("flex-1", className)}
      style={{ backgroundColor: colors.background }}
    >
      <WebView
        ref={webViewRef}
        source={{ uri: url }}
        style={{ flex: 1 }}
        onLoadStart={handleLoadStart}
        onLoadEnd={handleLoadEnd}
        onError={handleError}
        startInLoadingState={true}
        scalesPageToFit={true}
        javaScriptEnabled={true}
        domStorageEnabled={true}
        mixedContentMode="always"
        renderLoading={() => (
          <View
            className="flex-1 items-center justify-center"
            style={{ backgroundColor: colors.background }}
          >
            <ActivityIndicator
              size="large"
              color={colors.primary}
            />
            <Text className="mt-4 text-sm text-muted">
              Loading chat...
            </Text>
          </View>
        )}
      />
      {loading && !error && (
        <View
          className="absolute top-0 left-0 right-0 h-1"
          style={{ backgroundColor: colors.primary }}
        />
      )}
    </View>
  );
}
