import random
import datetime
import os
from dotenv import load_dotenv

class Person:
    def __init__(self, n, e, be):
        self.name = n
        self.email = e
        self.backupEmail = be
        self.matchedSender = False
        self.matchedReciever = False

def main():
    load_dotenv()

    resultsDict = {}

    theGroup = []
    year = str(datetime.datetime.now().year)

    fileName = os.getenv("FILENAME")
    file = open(fileName,"r")
    lines = file.readlines()

    for line in lines:
        parts = line.split(',')
        theGroup.append(Person(parts[0], parts[1], parts[2]))
    file.close()

    nPeople = len(theGroup)

    for run in range(0, 100000):
        # shuffle and check if anyone got matched with themselves
        # can plug in more rules here later i.e. no repeats from previous year
        isMatched = False
        while (isMatched == False):
            isMatched = True
            theGroupShuffled = theGroup.copy()
            random.shuffle(theGroupShuffled)

            for i in range(0, nPeople):
                if theGroup[i].name == theGroupShuffled[i].name:
                    isMatched = False

        #create a backup file just in case
        # file = open('backup ' + year + '.txt','a')
        # for i in range (0, nPeople):
        #     file.write('%s is buying a gift for %s.\n' % (theGroup[i].name, theGroupShuffled[i].name) )
        # file.close()

        for i in range (0, nPeople):
            person = theGroup[i].name
            matchedPerson = theGroupShuffled[i].name
            key = person + ' buys for ' + matchedPerson
            if (key in resultsDict):
                resultsDict[key] += 1
            else:
                resultsDict[key] = 1

            print(f"Results recorded for run: {run}")

    #log results to file and console
    file = open('analysisResults.txt','a')
    file.write('Results for run on ' + str(datetime.datetime.now()) + '\n')
    for key in sorted(resultsDict.keys()):
        file.write(f'{key} : {resultsDict[key]}\n')
        print(f'{key} : {resultsDict[key]}')
    file.close()
    
    print("Complete\n")
    exit(0)

if __name__ == "__main__":
    main()
