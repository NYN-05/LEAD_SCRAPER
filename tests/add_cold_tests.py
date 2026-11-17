content = open('c:/Users/JHASHANK/Downloads/AKSHARA_ASSIGNMENT/tests/test_photography_leads.py', 'r', encoding='utf-8').read()

cold_test = '''
        # COLD: Weak, indirect, or general photography interest without clear intent
        cold_cases = [
            "Photography tips and camera settings discussion.",
            "Just bought a new Canon EOS R5. Love the image quality!",
            "Beautiful sunset photo I took yesterday.",
            "Learning portrait photography techniques.",
            "Camera gear recommendations for beginners.",
        ]
        for text in cold_cases:
            result = classify_post({"post_text": text})
            self.assertEqual(result["lead_temperature"], "Cold",
                             f"Expected Cold for: {text}")
            self.assertFalse(result["qualified"],
                             f"Expected not qualified for: {text}")


if __name__ == "__main__":
    unittest.main()
'''

if 'if __name__' in content:
    content = content.replace('if __name__ == "__main__":', cold_test.replace('if __name__ == "__main__":', ''), 1)
else:
    content = content.rstrip() + cold_test

open('c:/Users/JHASHANK/Downloads/AKSHARA_ASSIGNMENT/tests/test_photography_leads.py', 'w', encoding='utf-8').write(content)
print('Added COLD test cases')