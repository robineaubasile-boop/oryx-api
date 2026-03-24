print("Analyse entreprise") 
entreprise = input("Nom de l'entreprise : ")  
roe = float(input("Entre le ROE (%) : ")) 
marge = float(input("Entre la marge nette (%) : ")) 
dette = float(input("Dette / Capitaux propres (%) : ")) 
score = 0 
# ROE 
if roe > 15: 
	score += 1 
# Marge 
if marge > 10: 
	score += 1 
# Dette 
if dette < 100: 
	score += 1 
# Verdict 
if score == 3: 
	verdict = "Excellente entreprise" 
elif score == 2: 
	verdict = "Entreprise correcte" 
else: 
	verdict = "Entreprise faible" 
print("Verdict :", verdict) 
# Sauvegarde historique 
fichier = open("historique.txt", "a") 
fichier.write("Entreprise : " + entreprise + "\n") 
fichier.write("ROE : " + str(roe) + "\n") 
fichier.write("Marge : " + str(marge) + "\n") 
fichier.write("Dette : " + str(dette) + "\n") 
fichier.write("Verdict : " + verdict + "\n") 
fichier.write("----------------------\n") 
fichier.close()
